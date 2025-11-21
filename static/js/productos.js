// JS para productos: búsqueda con debounce, filtro y confirm delete.
document.addEventListener('DOMContentLoaded', function() {
    const globalInput = document.getElementById('global_search_input');
    const scannerInput = document.getElementById('barcode_stock_input');
    const cantidadInput = document.getElementById('cantidad_stock_input');
    const form = document.getElementById('stockScannerForm');

    function debounce(fn, wait) {
        let t;
        return function(...args) {
            clearTimeout(t);
            t = setTimeout(() => fn.apply(this, args), wait);
        };
    }

    function filterTable(query) {
        const q = (query || '').toLowerCase().trim();
        const filas = document.querySelectorAll('#tablaInventario tbody tr');
        filas.forEach(fila => {
            const text = fila.innerText.toLowerCase();
            const match = q === '' ? true : text.includes(q);
            fila.style.display = match ? '' : 'none';

            // resaltar celdas que contienen la búsqueda
            fila.querySelectorAll('td').forEach(td => {
                const tdText = td.innerText.toLowerCase();
                if (q !== '' && tdText.includes(q)) {
                    td.classList.add('td-match');
                } else {
                    td.classList.remove('td-match');
                }
            });
        });
    }

    // Exponer para compatibilidad con onkeyup inline previo
    window.filtrarInventario = function() {
        const val = scannerInput ? scannerInput.value : '';
        filterTable(val);
    };

    if (globalInput) {
        globalInput.addEventListener('input', debounce(function(e) {
            filterTable(e.target.value);
        }, 220));
    }

    if (scannerInput) {
        // mantener comportamiento de submit al presionar Enter (scanner)
        scannerInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                if (scannerInput.value.trim() !== '' && parseInt(cantidadInput.value) > 0) {
                    form.submit();
                }
            }
        });

        // filtrar en tiempo real mientras se escribe (no interfiere con submit)
        scannerInput.addEventListener('input', debounce(function(e) {
            filterTable(e.target.value);
        }, 120));
    }

    // Confirmación de eliminación (no rompe la URL de backend)
    document.querySelectorAll('.btn-eliminar').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            const nombre = this.getAttribute('data-producto') || 'este producto';
            const href = this.href;
            if (typeof confirmAction === 'function') {
                confirmAction(`¿Seguro que desea eliminar "${nombre}"?`).then(ok => { if (ok) window.location = href; });
            } else {
                // Fallback a confirm nativo
                if (confirm(`¿Seguro que desea eliminar "${nombre}"?`)) window.location = href;
            }
        });
    });

});
