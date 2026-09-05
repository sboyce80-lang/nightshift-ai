#!/usr/bin/env python3
"""Offline tests for the PLG → CRM auto-create bridge (crm_sync.py).

Pins the load-bearing rules:

  - A fresh signup gets exactly one crm_account (linked, owned, prospect),
    one primary contact, and one [auto] timeline note.
  - The bridge is idempotent per org — re-running it (double-submit of the
    onboarding form, requeue) never duplicates anything.
  - An account a founder already created BY HAND with the same name gets
    LINKED, not duplicated — and hand-entered fields are never overwritten.
  - An account already linked to the org is left completely untouched.
  - plan stays NULL (the crm_accounts CHECK + CRM UI don't know freemium).
  - A bogus PLG_CRM_DEFAULT_OWNER degrades to unassigned, never a bad row.

Run: python3 test_crm_sync.py
"""
import os
import sys
import tempfile

_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="crmsync-test-"), "crm.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402
from db import engine, session_scope  # noqa: E402
from models import (  # noqa: E402
    Base, CrmAccount, CrmAccountNote, CrmContact, Organization,
    OrganizationMembership, User,
)
from crm_sync import ensure_crm_account_for_org  # noqa: E402

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  got={got!r} want={want!r}")


def _mk_org(session, name="Profeta Painting", domain="profetapainting.com",
            phone="555-0142", size="11-50"):
    org = Organization(name=name, email_domain=domain, plan="freemium",
                       is_beta_approved=True, phone=phone, company_size=size)
    session.add(org)
    session.flush()
    user = User(email=f"tony@{domain or 'example.com'}", name="Tony Profeta",
                title="Owner")
    session.add(user)
    session.flush()
    session.add(OrganizationMembership(
        organization_id=org.id, user_id=user.id, role="owner"))
    user.current_organization_id = org.id
    return org, user


def test_fresh_signup_creates_full_record():
    print("fresh signup:")
    with session_scope() as session:
        org, user = _mk_org(session)
        account, created = ensure_crm_account_for_org(session, org, user)
        check("created flag", created, True)
        check("account name", account.name, "Profeta Painting")
        check("linked org id", account.knightshift_org_id, org.id)
        check("status", account.status, "prospect")
        check("plan stays NULL", account.plan, None)
        check("owner defaulted", account.account_owner, "steve")
        check("phone copied", account.phone, "555-0142")
        check("website from domain", account.website,
              "https://profetapainting.com")

        contacts = session.query(CrmContact).filter_by(
            account_id=account.id).all()
        check("one contact", len(contacts), 1)
        check("contact is primary", contacts[0].is_primary, True)
        check("contact email", contacts[0].email, "tony@profetapainting.com")
        check("contact title", contacts[0].title, "Owner")
        check("lead source", contacts[0].lead_source, "plg_signup")

        notes = session.query(CrmAccountNote).filter_by(
            account_id=account.id).all()
        check("one note", len(notes), 1)
        check("note marked [auto]", notes[0].body.startswith("[auto]"), True)
        check("note mentions company size", "11-50" in notes[0].body, True)


def test_idempotent_per_org():
    print("idempotency:")
    with session_scope() as session:
        org, user = _mk_org(session, name="Twice Co", domain="twice.co")
        a1, c1 = ensure_crm_account_for_org(session, org, user)
        a2, c2 = ensure_crm_account_for_org(session, org, user)
        check("same account returned", a1.id, a2.id)
        check("second call created nothing", c2, False)
        n_accounts = session.query(CrmAccount).filter_by(
            knightshift_org_id=org.id).count()
        n_contacts = session.query(CrmContact).filter_by(
            account_id=a1.id).count()
        n_notes = session.query(CrmAccountNote).filter_by(
            account_id=a1.id).count()
        check("one account", n_accounts, 1)
        check("one contact", n_contacts, 1)
        check("one note", n_notes, 1)


def test_links_existing_manual_account():
    print("manual account gets linked, not duplicated:")
    with session_scope() as session:
        manual = CrmAccount(name="Handmade Painting", status="beta",
                            contact_status="contacted",
                            account_owner="matt", phone="999-1111",
                            notes="Met at trade show")
        session.add(manual)
        session.flush()
        org, user = _mk_org(session, name="handmade painting",
                            domain="handmadepainting.com", phone="555-2222")
        account, created = ensure_crm_account_for_org(session, org, user)
        check("no new account created", created, False)
        check("linked the manual row", account.id, manual.id)
        check("org link set", account.knightshift_org_id, org.id)
        check("hand-set status kept", account.status, "beta")
        check("hand-set owner kept", account.account_owner, "matt")
        check("hand-set phone kept", account.phone, "999-1111")
        check("hand-written notes kept", account.notes, "Met at trade show")
        contacts = session.query(CrmContact).filter_by(
            account_id=account.id).all()
        check("signup contact added", len(contacts), 1)
        notes = session.query(CrmAccountNote).filter_by(
            account_id=account.id).count()
        check("link recorded on timeline", notes, 1)


def test_already_linked_account_untouched():
    print("already-linked account untouched:")
    with session_scope() as session:
        org, user = _mk_org(session, name="Settled Co", domain="settled.com")
        linked = CrmAccount(name="Settled Co (enriched)", status="client",
                            knightshift_org_id=org.id, account_owner="brian")
        session.add(linked)
        session.flush()
        account, created = ensure_crm_account_for_org(session, org, user)
        check("returns the linked row", account.id, linked.id)
        check("nothing created", created, False)
        check("enriched name kept", account.name, "Settled Co (enriched)")
        check("status kept", account.status, "client")
        n_notes = session.query(CrmAccountNote).filter_by(
            account_id=account.id).count()
        n_contacts = session.query(CrmContact).filter_by(
            account_id=account.id).count()
        check("no note added", n_notes, 0)
        check("no contact added", n_contacts, 0)


def test_bad_owner_degrades_to_unassigned():
    print("bad default owner:")
    orig = config.PLG_CRM_DEFAULT_OWNER
    config.PLG_CRM_DEFAULT_OWNER = "elliot"  # 0018 renamed this to elliott
    try:
        with session_scope() as session:
            org, user = _mk_org(session, name="Ownerless Co",
                                domain="ownerless.com")
            account, created = ensure_crm_account_for_org(session, org, user)
            check("account still created", created, True)
            check("owner left unassigned", account.account_owner, None)
    finally:
        config.PLG_CRM_DEFAULT_OWNER = orig


def test_no_domain_org():
    print("free-email org (no domain):")
    with session_scope() as session:
        org, user = _mk_org(session, name="Solo Painter", domain=None)
        account, created = ensure_crm_account_for_org(session, org, user)
        check("created", created, True)
        check("no website invented", account.website, None)


def main():
    Base.metadata.create_all(engine)
    test_fresh_signup_creates_full_record()
    test_idempotent_per_org()
    test_links_existing_manual_account()
    test_already_linked_account_untouched()
    test_bad_owner_degrades_to_unassigned()
    test_no_domain_org()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
