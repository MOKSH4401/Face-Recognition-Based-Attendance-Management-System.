/**
 * Attendance Reports — filters + PDF / Excel / CSV export.
 */
(function () {
    'use strict';

    function getFilterParams() {
        var form = document.getElementById('filter-form');
        var params = new URLSearchParams();
        if (!form) return params;

        var elements = form.elements;
        for (var i = 0; i < elements.length; i++) {
            var el = elements[i];
            if (!el.name || el.disabled) continue;
            if (el.type === 'submit' || el.type === 'button') continue;
            if (el.value !== null && el.value !== undefined && String(el.value).length) {
                params.append(el.name, el.value);
            }
        }
        return params;
    }

    function setExportEnabled(enabled) {
        ['btn-export-pdf', 'btn-export-excel', 'btn-export-csv'].forEach(function (id) {
            var btn = document.getElementById(id);
            if (btn) btn.disabled = !enabled;
        });
    }

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function updateTable(data) {
        var tbody = document.getElementById('attendance-data');
        var countEl = document.getElementById('record-count');
        if (!tbody) return;

        if (!data || !data.length) {
            tbody.innerHTML =
                '<tr><td colspan="6" class="text-center text-muted py-4">' +
                'No records found matching the criteria.</td></tr>';
            if (countEl) countEl.textContent = '0';
            setExportEnabled(false);
            return;
        }

        var html = '';
        data.forEach(function (item) {
            html +=
                '<tr>' +
                '<td><div class="fw-semibold">' + escapeHtml(item.student_name) + '</div>' +
                '<small class="text-muted">' + escapeHtml(item.student_code || '') + '</small></td>' +
                '<td><div>' + escapeHtml(item.course_name) + '</div>' +
                '<small class="text-muted">' + escapeHtml(item.course_code || '') + '</small></td>' +
                '<td><small>' + escapeHtml(item.faculty_names || '—') + '</small></td>' +
                '<td><small>' + escapeHtml(item.departments || '—') + '</small></td>' +
                '<td>' + escapeHtml(item.date || '') + '</td>' +
                '<td>' + escapeHtml(item.time || '') + '</td>' +
                '</tr>';
        });
        tbody.innerHTML = html;
        if (countEl) countEl.textContent = String(data.length);
        setExportEnabled(true);
    }

    function loadReport() {
        var tbody = document.getElementById('attendance-data');
        if (tbody) {
            tbody.innerHTML =
                '<tr><td colspan="6" class="text-center text-muted py-4">' +
                '<i class="fas fa-spinner fa-spin me-2"></i>Loading...</td></tr>';
        }
        setExportEnabled(false);

        var params = getFilterParams();
        fetch('/get_attendance_data?' + params.toString(), { credentials: 'same-origin' })
            .then(function (res) { return res.json(); })
            .then(function (result) {
                if (result.status === 'success') {
                    updateTable(result.data || []);
                } else {
                    if (tbody) {
                        tbody.innerHTML =
                            '<tr><td colspan="6" class="text-center text-danger py-4">' +
                            escapeHtml(result.message || 'Failed to load report') +
                            '</td></tr>';
                    }
                    var countEl = document.getElementById('record-count');
                    if (countEl) countEl.textContent = '0';
                }
            })
            .catch(function (err) {
                console.error(err);
                if (tbody) {
                    tbody.innerHTML =
                        '<tr><td colspan="6" class="text-center text-danger py-4">' +
                        'Error fetching data. Please try again.</td></tr>';
                }
            });
    }

    function exportReport(fmt) {
        var params = getFilterParams();
        window.location.href = '/api/reports/export/' + fmt + '?' + params.toString();
    }

    function resetFilters() {
        var form = document.getElementById('filter-form');
        if (!form) return;
        form.reset();
        setDefaultDates();
        var tbody = document.getElementById('attendance-data');
        if (tbody) {
            tbody.innerHTML =
                '<tr><td colspan="6" class="text-center text-muted py-4">' +
                'Set filters and click <strong>Generate Report</strong>.</td></tr>';
        }
        var countEl = document.getElementById('record-count');
        if (countEl) countEl.textContent = '0';
        setExportEnabled(false);
    }

    function setDefaultDates() {
        var today = new Date();
        var firstDay = new Date(today.getFullYear(), today.getMonth(), 1);
        var fromEl = document.getElementById('date-from');
        var toEl = document.getElementById('date-to');
        if (fromEl) fromEl.valueAsDate = firstDay;
        if (toEl) toEl.valueAsDate = today;
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!document.getElementById('filter-form')) return;

        setDefaultDates();

        document.getElementById('filter-form').addEventListener('submit', function (e) {
            e.preventDefault();
            loadReport();
        });

        var resetBtn = document.getElementById('btn-reset-filters');
        if (resetBtn) resetBtn.addEventListener('click', resetFilters);

        var pdfBtn = document.getElementById('btn-export-pdf');
        if (pdfBtn) pdfBtn.addEventListener('click', function () { exportReport('pdf'); });

        var excelBtn = document.getElementById('btn-export-excel');
        if (excelBtn) excelBtn.addEventListener('click', function () { exportReport('excel'); });

        var csvBtn = document.getElementById('btn-export-csv');
        if (csvBtn) csvBtn.addEventListener('click', function () { exportReport('csv'); });
    });
})();
