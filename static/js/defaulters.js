/**
 * Defaulter Management — search, filter, table, Excel/PDF export.
 */
(function () {
    'use strict';

    const API_URL = '/api/defaulters';

    function getFilters() {
        return {
            search: (document.getElementById('search-input') || {}).value || '',
            course_id: (document.getElementById('course-filter') || {}).value || 'all',
            status: (document.getElementById('status-filter') || {}).value || 'all'
        };
    }

    function buildQuery(filters) {
        const params = new URLSearchParams();
        if (filters.search && filters.search.trim()) {
            params.set('search', filters.search.trim());
        }
        if (filters.course_id && filters.course_id !== 'all') {
            params.set('course_id', filters.course_id);
        }
        if (filters.status) {
            params.set('status', filters.status);
        }
        const qs = params.toString();
        return qs ? ('?' + qs) : '';
    }

    function showError(message) {
        const box = document.getElementById('defaulter-error');
        if (!box) return;
        box.textContent = message;
        box.classList.remove('d-none');
    }

    function clearError() {
        const box = document.getElementById('defaulter-error');
        if (!box) return;
        box.classList.add('d-none');
        box.textContent = '';
    }

    function statusBadge(status) {
        if (status === 'Defaulter') {
            return '<span class="badge bg-danger">Defaulter</span>';
        }
        if (status === 'Satisfactory') {
            return '<span class="badge bg-success">Satisfactory</span>';
        }
        return '<span class="badge bg-secondary">No Data</span>';
    }

    function pctDisplay(pct) {
        if (pct === null || pct === undefined) {
            return '<span class="text-muted">N/A</span>';
        }
        var cls = pct < 75 ? 'text-danger fw-bold' : 'text-success fw-semibold';
        return '<span class="' + cls + '">' + pct + '%</span>';
    }

    function updateSummary(summary) {
        if (!summary) return;
        setText('kpi-defaulter-count', summary.defaulter_count);
        setText('kpi-satisfactory-count', summary.satisfactory_count);
        setText('kpi-total-records', summary.total_records);
        setText(
            'kpi-avg-percentage',
            summary.average_percentage !== null && summary.average_percentage !== undefined
                ? summary.average_percentage + '%'
                : '—'
        );
    }

    function setText(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function renderTable(records) {
        var tbody = document.getElementById('defaulters-tbody');
        var countEl = document.getElementById('record-count');
        if (countEl) countEl.textContent = records.length;

        if (!records.length) {
            tbody.innerHTML =
                '<tr><td colspan="7" class="text-center text-muted py-4">' +
                'No records match your filters.</td></tr>';
            return;
        }

        var html = '';
        records.forEach(function (rec) {
            var rowClass = rec.status === 'Defaulter' ? 'table-danger' : '';
            var emailBtn =
                '<button type="button" class="btn btn-sm btn-outline-warning btn-send-email" ' +
                'data-student-id="' + rec.student_id + '" data-course-id="' + rec.course_id + '" ' +
                'title="Send warning email">' +
                '<i class="fas fa-envelope"></i></button>';
            html +=
                '<tr class="' + rowClass + '">' +
                '<td><div class="fw-semibold">' + escapeHtml(rec.student_name) + '</div>' +
                '<small class="text-muted">' + escapeHtml(rec.student_code) + '</small></td>' +
                '<td><div>' + escapeHtml(rec.course_name) + '</div>' +
                '<small class="text-muted">' + escapeHtml(rec.course_code) + '</small></td>' +
                '<td>' + pctDisplay(rec.attendance_percentage) + '</td>' +
                '<td><small>' + rec.present_sessions + ' / ' + rec.total_sessions + '</small></td>' +
                '<td>' + statusBadge(rec.status) + '</td>' +
                '<td><small>' + escapeHtml(rec.reason) + '</small></td>' +
                '<td>' + emailBtn + '</td>' +
                '</tr>';
        });
        tbody.innerHTML = html;

        tbody.querySelectorAll('.btn-send-email').forEach(function (btn) {
            btn.addEventListener('click', function () {
                sendOneEmail(btn.getAttribute('data-student-id'), btn.getAttribute('data-course-id'), btn);
            });
        });
    }

    function sendOneEmail(studentId, courseId, btn) {
        if (!confirm('Send attendance warning email to this student?')) return;
        if (btn) btn.disabled = true;
        fetch('/api/email/send-defaulter', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ student_id: studentId, course_id: courseId })
        })
            .then(function (res) { return res.json(); })
            .then(function (data) {
                alert(data.message || (data.ok ? 'Email sent.' : 'Failed to send email.'));
            })
            .catch(function (err) {
                alert('Request failed: ' + err);
            })
            .finally(function () {
                if (btn) btn.disabled = false;
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

    function loadDefaulters() {
        clearError();
        var filters = getFilters();
        var tbody = document.getElementById('defaulters-tbody');
        if (tbody) {
            tbody.innerHTML =
                '<tr><td colspan="7" class="text-center text-muted py-4">' +
                '<i class="fas fa-spinner fa-spin me-2"></i>Loading...</td></tr>';
        }

        fetch(API_URL + buildQuery(filters), { credentials: 'same-origin' })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (payload) {
                if (payload.status !== 'success') {
                    throw new Error(payload.message || 'Failed to load defaulters');
                }
                updateSummary(payload.data.summary);
                renderTable(payload.data.records || []);
                var updated = document.getElementById('defaulter-updated');
                if (updated && payload.data.generated_at) {
                    updated.textContent =
                        '· Updated ' + new Date(payload.data.generated_at).toLocaleString();
                }
            })
            .catch(function (err) {
                console.error(err);
                showError('Could not load defaulter data. Please try again.');
                if (tbody) {
                    tbody.innerHTML =
                        '<tr><td colspan="7" class="text-center text-danger py-4">' +
                        'Failed to load data.</td></tr>';
                }
            });
    }

    function exportFile(kind) {
        var filters = getFilters();
        var url = '/api/defaulters/export/' + kind + buildQuery(filters);
        window.location.href = url;
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!document.getElementById('defaulters-table')) return;

        loadDefaulters();

        var form = document.getElementById('defaulter-filter-form');
        if (form) {
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                loadDefaulters();
            });
        }

        // Live search with debounce
        var searchInput = document.getElementById('search-input');
        var searchTimer = null;
        if (searchInput) {
            searchInput.addEventListener('input', function () {
                clearTimeout(searchTimer);
                searchTimer = setTimeout(loadDefaulters, 350);
            });
        }

        ['course-filter', 'status-filter'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.addEventListener('change', loadDefaulters);
        });

        var refreshBtn = document.getElementById('btn-refresh-defaulters');
        if (refreshBtn) refreshBtn.addEventListener('click', loadDefaulters);

        var excelBtn = document.getElementById('btn-export-excel');
        if (excelBtn) excelBtn.addEventListener('click', function () { exportFile('excel'); });

        var pdfBtn = document.getElementById('btn-export-pdf');
        if (pdfBtn) pdfBtn.addEventListener('click', function () { exportFile('pdf'); });

        var emailAllBtn = document.getElementById('btn-email-all-defaulters');
        if (emailAllBtn) {
            emailAllBtn.addEventListener('click', function () {
                if (!confirm('Send warning emails to all current defaulters?')) return;
                emailAllBtn.disabled = true;
                var filters = getFilters();
                fetch('/api/email/send-all-defaulters', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({
                        course_id: filters.course_id && filters.course_id !== 'all' ? filters.course_id : 'all'
                    })
                })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        alert(data.message || 'Done');
                    })
                    .catch(function (err) {
                        alert('Request failed: ' + err);
                    })
                    .finally(function () {
                        emailAllBtn.disabled = false;
                    });
            });
        }
    });
})();
