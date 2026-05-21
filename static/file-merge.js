/* Knight Shift — file input merge helper.
 *
 * A native <input type="file"> REPLACES its whole selection every time the
 * user picks files through the Browse dialog. wireFileMerge keeps a persistent
 * accumulator so Browse — and drag-drop — ADD to the selection instead of
 * clobbering it. Picking the same file twice is de-duplicated.
 *
 * wireFileMerge(input, opts) -> { reset }
 *   opts.dropZone — element that should accept drag-drop (optional)
 *   opts.listEl   — element to render the chosen-file list into (optional)
 *   opts.onChange — callback(files[]) fired after every change (optional)
 */
(function () {
    'use strict';

    function fileKey(f) {
        return f.name + '|' + f.size + '|' + f.lastModified;
    }

    function isPDF(f) {
        return f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf');
    }

    window.wireFileMerge = function (input, opts) {
        opts = opts || {};
        const dropZone = opts.dropZone || null;
        const listEl = opts.listEl || null;
        const onChange = opts.onChange || function () {};

        // Persistent accumulator — survives the native FileList replacement
        // that the browser performs on every Browse-dialog selection.
        let files = [];

        function commit() {
            const dt = new DataTransfer();
            files.forEach(f => dt.items.add(f));
            input.files = dt.files;   // programmatic assignment does not fire 'change'
            renderList();
            onChange(files.slice());
        }

        function merge(incoming) {
            const seen = new Set(files.map(fileKey));
            for (const f of incoming) {
                const k = fileKey(f);
                if (seen.has(k)) continue;
                seen.add(k);
                files.push(f);
            }
            commit();
        }

        function removeAt(i) {
            files.splice(i, 1);
            commit();
        }

        function renderList() {
            if (!listEl) return;
            listEl.innerHTML = '';
            files.forEach((f, i) => {
                const sizeMB = (f.size / 1048576).toFixed(1);
                const item = document.createElement('div');
                item.className = 'file-item';
                if (!isPDF(f)) item.style.borderLeft = '3px solid #dc2626';

                const name = document.createElement('span');
                name.className = 'file-name';
                name.textContent = (isPDF(f) ? '' : '⚠ ') + f.name;

                const size = document.createElement('span');
                size.className = 'file-size';
                size.textContent = sizeMB + ' MB';

                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'file-remove';
                remove.setAttribute('aria-label', 'Remove ' + f.name);
                remove.textContent = '×';
                remove.addEventListener('click', () => removeAt(i));

                item.appendChild(name);
                item.appendChild(size);
                item.appendChild(remove);
                listEl.appendChild(item);
            });
        }

        // Browse dialog (and a drop straight onto the input) — at this point
        // input.files holds ONLY the just-picked native selection, so absorb
        // it into the accumulator instead of letting it replace everything.
        input.addEventListener('change', () => {
            merge(Array.from(input.files || []));
        });

        if (dropZone) {
            ['dragenter', 'dragover'].forEach(evt =>
                dropZone.addEventListener(evt, e => {
                    e.preventDefault();
                    dropZone.classList.add('drag-over');
                }));
            ['dragleave', 'drop'].forEach(evt =>
                dropZone.addEventListener(evt, e => {
                    e.preventDefault();
                    dropZone.classList.remove('drag-over');
                }));
            dropZone.addEventListener('drop', e => {
                if (!e.dataTransfer || e.dataTransfer.files.length === 0) return;
                merge(Array.from(e.dataTransfer.files));
            });
        }

        return {
            reset: function () { files = []; commit(); },
        };
    };
})();
