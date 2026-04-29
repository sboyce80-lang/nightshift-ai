/* Knight Shift — browser-direct R2 multipart uploader.
 *
 * Upload pipeline:
 *   1. POST /api/uploads/init with [{filename, size}, …]
 *   2. For each file, PUT each part to its presigned URL in parallel (max
 *      MAX_PARALLEL parts in flight at once). Collect ETags from response
 *      headers — R2 must have CORS configured to expose `ETag`.
 *   3. POST /api/uploads/complete per file with the parts list.
 *   4. Set hidden `uploaded_manifest` field on the form and submit it.
 *
 * Why XHR not fetch: fetch() has no upload progress events. We need a
 * per-part progress update to compose a per-file progress bar.
 *
 * Public API exposed on window.NSUpload:
 *   driveAndSubmit(form, fileInput, opts) — wires onSubmit so that clicking
 *     submit kicks off uploads and only submits the form once they're done.
 *     opts = { onProgress(fileIdx, bytesDone, totalBytes),
 *              onError(message), onPhase(text) }.
 */
(function () {
    'use strict';

    const MAX_PARALLEL_PARTS = 3;       // concurrent PUTs per file
    const PART_RETRY_LIMIT = 3;          // retry a failed part this many times before giving up

    function putPart(url, blob, onProgress) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('PUT', url, true);
            xhr.upload.onprogress = e => {
                if (e.lengthComputable) onProgress(e.loaded);
            };
            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    // R2 returns the per-part ETag in the response headers.
                    // The ETag is a quoted hex string; we forward it as-is.
                    const etag = xhr.getResponseHeader('ETag');
                    if (!etag) {
                        reject(new Error('R2 did not return an ETag header — check bucket CORS (must expose ETag).'));
                        return;
                    }
                    resolve(etag);
                } else {
                    reject(new Error(`Part upload failed (HTTP ${xhr.status}). ${xhr.responseText || ''}`.trim()));
                }
            };
            xhr.onerror = () => reject(new Error('Network error during part upload.'));
            xhr.onabort = () => reject(new Error('Part upload aborted.'));
            xhr.send(blob);
        });
    }

    async function putPartWithRetry(url, blob, onProgress) {
        let lastErr;
        for (let attempt = 1; attempt <= PART_RETRY_LIMIT; attempt++) {
            try {
                return await putPart(url, blob, onProgress);
            } catch (err) {
                lastErr = err;
                // Backoff: 1s, 2s, 4s.
                await new Promise(r => setTimeout(r, 1000 * Math.pow(2, attempt - 1)));
            }
        }
        throw lastErr;
    }

    /**
     * Upload a single file as a multipart upload. `desc` comes from
     * /api/uploads/init and contains key, upload_id, size, parts: [{part_number, url}].
     * `onBytes` is called with the running total of bytes uploaded for this file.
     */
    async function uploadFile(file, desc, partSize, onBytes) {
        const partProgress = new Array(desc.parts.length).fill(0);
        const reportProgress = () => {
            const total = partProgress.reduce((a, b) => a + b, 0);
            onBytes(total);
        };

        const completedParts = [];
        let partsInFlight = 0;
        let nextIdx = 0;
        let aborted = false;

        return new Promise((resolve, reject) => {
            const fail = (err) => {
                if (aborted) return;
                aborted = true;
                reject(err);
            };

            const launchNext = () => {
                if (aborted) return;
                while (partsInFlight < MAX_PARALLEL_PARTS && nextIdx < desc.parts.length) {
                    const idx = nextIdx++;
                    const partInfo = desc.parts[idx];
                    const start = idx * partSize;
                    const end = Math.min(file.size, start + partSize);
                    const blob = file.slice(start, end);
                    const expectedBytes = end - start;
                    partsInFlight++;
                    putPartWithRetry(partInfo.url, blob, (loaded) => {
                        partProgress[idx] = Math.min(loaded, expectedBytes);
                        reportProgress();
                    }).then((etag) => {
                        partProgress[idx] = expectedBytes;
                        reportProgress();
                        completedParts.push({ part_number: partInfo.part_number, etag });
                        partsInFlight--;
                        if (completedParts.length === desc.parts.length) {
                            // All parts done.
                            resolve(completedParts);
                        } else {
                            launchNext();
                        }
                    }).catch(fail);
                }
            };
            launchNext();
        });
    }

    async function postJSON(path, body) {
        const res = await fetch(path, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = (data && data.error) || `Request to ${path} failed (HTTP ${res.status}).`;
            const err = new Error(msg);
            err.status = res.status;
            throw err;
        }
        return data;
    }

    /**
     * Drive the full upload pipeline for the files in `fileInput`. Returns
     * the manifest object that should be JSON-stringified into the hidden
     * `uploaded_manifest` form field.
     */
    async function uploadAll(fileInput, opts) {
        const files = Array.from(fileInput.files || []);
        if (!files.length) throw new Error('No files selected.');

        const onPhase = opts.onPhase || (() => {});
        const onProgress = opts.onProgress || (() => {});

        onPhase('Requesting upload URLs…');
        const initResp = await postJSON('/api/uploads/init', {
            files: files.map(f => ({ filename: f.name, size: f.size })),
        });
        const partSize = initResp.part_size;

        // Track per-file totals so we can report bytes done per file.
        const fileTotals = files.map(f => f.size);

        // Upload files sequentially. Within a file we upload parts in
        // parallel — that's where the speedup comes from. Doing files in
        // parallel too would multiply concurrency and likely just thrash.
        const manifestFiles = [];
        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            const desc = initResp.uploads[i];
            onPhase(`Uploading ${file.name} (${(file.size / 1048576).toFixed(1)} MB)…`);

            try {
                const parts = await uploadFile(file, desc, partSize, (bytes) => {
                    onProgress(i, bytes, fileTotals[i]);
                });
                onPhase(`Finalizing ${file.name}…`);
                await postJSON('/api/uploads/complete', {
                    key: desc.key,
                    upload_id: desc.upload_id,
                    parts,
                });
                manifestFiles.push({ filename: desc.filename, key: desc.key });
            } catch (err) {
                // Best-effort cleanup so abandoned multiparts don't linger.
                try {
                    await postJSON('/api/uploads/abort', {
                        key: desc.key,
                        upload_id: desc.upload_id,
                    });
                } catch (_) { /* swallow */ }
                throw err;
            }
        }

        return {
            submission_id: initResp.submission_id,
            files: manifestFiles,
        };
    }

    /**
     * Wire a form so its submit button kicks off the upload pipeline before
     * the form actually POSTs. The form must contain a hidden input named
     * `uploaded_manifest` and a file input.
     */
    function driveAndSubmit(form, fileInput, opts) {
        opts = opts || {};
        const manifestField = form.querySelector('input[name="uploaded_manifest"]');
        if (!manifestField) {
            console.error('NSUpload: form is missing <input name="uploaded_manifest">');
            return;
        }
        let inFlight = false;

        form.addEventListener('submit', async (e) => {
            if (inFlight) { e.preventDefault(); return; }
            const files = Array.from(fileInput.files || []);
            if (!files.length) return;            // let server flash "please upload"

            // Intercept submit until uploads finish.
            e.preventDefault();
            inFlight = true;

            // Strip the file input's `required` attribute and clear its files
            // before the eventual real submit, so the browser doesn't try to
            // re-upload them as multipart form data (we already pushed them
            // to R2). We do this AFTER capturing `files` above.
            const wasRequired = fileInput.hasAttribute('required');
            try {
                const manifest = await uploadAll(fileInput, opts);
                manifestField.value = JSON.stringify(manifest);

                if (wasRequired) fileInput.removeAttribute('required');
                // Replacing the input's files with an empty FileList is awkward
                // cross-browser. Easiest: disable the input so it isn't sent.
                fileInput.disabled = true;

                // Submit for real. We can't call form.submit() inside the
                // listener that just called preventDefault without it
                // re-entering this handler, so do it on the next tick.
                setTimeout(() => form.submit(), 0);
            } catch (err) {
                inFlight = false;
                if (wasRequired) fileInput.setAttribute('required', '');
                fileInput.disabled = false;
                if (opts.onError) opts.onError(err.message || String(err));
                else alert('Upload failed: ' + (err.message || err));
            }
        });
    }

    window.NSUpload = { driveAndSubmit, uploadAll };
})();
