/**
 * Global Search — filters, sorting, pagination, responsive results.
 */
(function () {
    'use strict';

    var state = {
        page: 1,
        debounceTimer: null
    };

    var TYPE_META = {
        student: { label: 'Student', badge: 'bg-primary', icon: 'fa-user-graduate' },
        faculty: { label: 'Faculty', badge: 'bg-info text-dark', icon: 'fa-chalkboard-teacher' },
        course: { label: 'Course', badge: 'bg-success', icon: 'fa-book' },
        department: { label: 'Department', badge: 'bg-warning text-dark', icon: 'fa-building' },
        attendance: { label: 'Attendance', badge: 'bg-danger', icon: 'fa-calendar-check' }
    };

    function el(id) {
        return document.getElementById(id);
    }

    function escapeHtml(str) {
        if (str == null) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function getFilters() {
        var facultyEl = el('search-faculty');
        return {
            q: (el('search-q') || {}).value || '',
            type: (el('search-type') || {}).value || 'all',
            course_id: (el('search-course') || {}).value || 'all',
            department: (el('search-department') || {}).value || '',
            faculty_id: facultyEl ? facultyEl.value : 'all',
            date_from: (el('search-date-from') || {}).value || '',
            date_to: (el('search-date-to') || {}).value || '',
            sort_by: (el('search-sort-by') || {}).value || 'relevance',
            sort_dir: (el('search-sort-dir') || {}).value || 'asc',
            per_page: (el('search-per-page') || {}).value || '20',
            page: state.page
        };
    }

    function buildQuery(filters) {
        var params = new URLSearchParams();
        Object.keys(filters).forEach(function (key) {
            var val = filters[key];
            if (val === undefined || val === null || val === '') return;
            if (key === 'course_id' && val === 'all') return;
            if (key === 'faculty_id' && val === 'all') return;
            params.set(key, val);
        });
        return params.toString();
    }

    function showError(msg) {
        var box = el('search-error');
        if (!box) return;
        if (!msg) {
            box.classList.add('d-none');
            box.textContent = '';
            return;
        }
        box.textContent = msg;
        box.classList.remove('d-none');
    }

    function setLoading(on) {
        var loading = el('search-loading');
        if (loading) loading.classList.toggle('d-none', !on);
    }

    function updateChips(counts, total) {
        var chips = document.querySelectorAll('.search-chip');
        chips.forEach(function (chip) {
            var t = chip.getAttribute('data-type');
            var n = chip.querySelector('.chip-n');
            if (!n) return;
            if (t === 'all') {
                n.textContent = total != null ? total : 0;
            } else {
                n.textContent = (counts && counts[t] != null) ? counts[t] : 0;
            }
        });
    }

    function renderRow(item) {
        var meta = TYPE_META[item.type] || { label: item.type, badge: 'bg-secondary', icon: 'fa-circle' };
        var link = item.link
            ? '<a class="btn btn-sm btn-outline-primary" href="' + escapeHtml(item.link) + '">Open</a>'
            : '<span class="text-muted small">—</span>';
        return (
            '<tr>' +
            '<td><span class="badge ' + meta.badge + '"><i class="fas ' + meta.icon + ' me-1"></i>' +
            escapeHtml(meta.label) + '</span></td>' +
            '<td><strong>' + escapeHtml(item.title) + '</strong>' +
            '<div class="small text-muted">' + escapeHtml(item.subtitle || '') + '</div></td>' +
            '<td class="d-none d-md-table-cell"><span class="small">' + escapeHtml(item.meta || '') +
            '</span>' + (item.extra ? '<div class="small text-muted">' + escapeHtml(item.extra) + '</div>' : '') +
            '</td>' +
            '<td class="d-none d-lg-table-cell">' + escapeHtml(item.department || '—') + '</td>' +
            '<td><small>' + escapeHtml(item.date || '—') + '</small></td>' +
            '<td class="text-end">' + link + '</td>' +
            '</tr>'
        );
    }

    function renderCard(item) {
        var meta = TYPE_META[item.type] || { label: item.type, badge: 'bg-secondary', icon: 'fa-circle' };
        var link = item.link
            ? '<a class="btn btn-sm btn-outline-primary mt-2" href="' + escapeHtml(item.link) + '">Open</a>'
            : '';
        return (
            '<div class="search-result-card border-bottom p-3">' +
            '<div class="d-flex justify-content-between align-items-start gap-2">' +
            '<div>' +
            '<span class="badge ' + meta.badge + ' mb-1"><i class="fas ' + meta.icon + ' me-1"></i>' +
            escapeHtml(meta.label) + '</span>' +
            '<div class="fw-semibold">' + escapeHtml(item.title) + '</div>' +
            '<div class="small text-muted">' + escapeHtml(item.subtitle || '') + '</div>' +
            '<div class="small mt-1">' + escapeHtml(item.meta || '') + '</div>' +
            (item.department ? '<div class="small text-muted">' + escapeHtml(item.department) + '</div>' : '') +
            (item.date ? '<div class="small">' + escapeHtml(item.date) + '</div>' : '') +
            link +
            '</div></div></div>'
        );
    }

    function renderPagination(meta) {
        var ul = el('search-pagination');
        if (!ul) return;
        ul.innerHTML = '';
        var totalPages = meta.total_pages || 1;
        var page = meta.page || 1;

        function addItem(label, targetPage, disabled, active) {
            var li = document.createElement('li');
            li.className = 'page-item' + (disabled ? ' disabled' : '') + (active ? ' active' : '');
            var a = document.createElement('a');
            a.className = 'page-link';
            a.href = '#';
            a.textContent = label;
            if (!disabled && !active) {
                a.addEventListener('click', function (e) {
                    e.preventDefault();
                    state.page = targetPage;
                    loadSearch();
                });
            }
            li.appendChild(a);
            ul.appendChild(li);
        }

        addItem('Prev', page - 1, page <= 1, false);
        var start = Math.max(1, page - 2);
        var end = Math.min(totalPages, page + 2);
        for (var i = start; i <= end; i++) {
            addItem(String(i), i, false, i === page);
        }
        addItem('Next', page + 1, page >= totalPages, false);
    }

    function renderResults(data) {
        var results = data.results || [];
        var meta = data.meta || {};
        var body = el('search-results-body');
        var cards = el('search-cards');
        var wrap = el('search-table-wrap');
        var empty = el('search-empty');
        var countBadge = el('search-result-count');

        updateChips(meta.counts || {}, meta.total || 0);

        if (countBadge) {
            countBadge.textContent = (meta.total || 0) + ' result' + ((meta.total === 1) ? '' : 's');
        }

        if (!results.length) {
            if (wrap) wrap.classList.add('d-none');
            if (cards) {
                cards.classList.add('d-none');
                cards.innerHTML = '';
            }
            if (empty) {
                empty.classList.remove('d-none');
                empty.innerHTML =
                    '<i class="fas fa-inbox fa-2x mb-2 opacity-50"></i>' +
                    '<p class="mb-0">No matches. Try different filters or dates.</p>';
            }
            renderPagination({ page: 1, total_pages: 1 });
            return;
        }

        if (empty) empty.classList.add('d-none');
        if (wrap) wrap.classList.remove('d-none');
        if (cards) {
            cards.classList.remove('d-none');
            cards.classList.add('d-md-none');
        }

        if (body) {
            body.innerHTML = results.map(renderRow).join('');
        }
        if (cards) {
            cards.innerHTML = results.map(renderCard).join('');
        }
        renderPagination(meta);
    }

    function loadSearch() {
        showError('');
        setLoading(true);
        var empty = el('search-empty');
        if (empty) empty.classList.add('d-none');

        var qs = buildQuery(getFilters());
        fetch('/api/search?' + qs, { credentials: 'same-origin' })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (payload) {
                setLoading(false);
                if (!payload.ok || payload.data.status === 'error') {
                    showError((payload.data && payload.data.message) || 'Search failed.');
                    return;
                }
                renderResults(payload.data);
            })
            .catch(function (err) {
                setLoading(false);
                console.error(err);
                showError('Unable to reach search API.');
            });
    }

    function resetForm() {
        var form = el('global-search-form');
        if (form) form.reset();
        if (el('search-type')) el('search-type').value = 'all';
        if (el('search-sort-by')) el('search-sort-by').value = 'relevance';
        if (el('search-sort-dir')) el('search-sort-dir').value = 'asc';
        if (el('search-per-page')) el('search-per-page').value = '20';
        state.page = 1;
        loadSearch();
    }

    function bind() {
        var form = el('global-search-form');
        if (!form) return;

        form.addEventListener('submit', function (e) {
            e.preventDefault();
            state.page = 1;
            loadSearch();
        });

        var resetBtn = el('btn-search-reset');
        if (resetBtn) resetBtn.addEventListener('click', resetForm);

        var qInput = el('search-q');
        if (qInput) {
            qInput.addEventListener('input', function () {
                clearTimeout(state.debounceTimer);
                state.debounceTimer = setTimeout(function () {
                    state.page = 1;
                    loadSearch();
                }, 400);
            });
        }

        ['search-type', 'search-course', 'search-department', 'search-faculty',
            'search-date-from', 'search-date-to', 'search-sort-by', 'search-sort-dir',
            'search-per-page'].forEach(function (id) {
            var node = el(id);
            if (!node) return;
            node.addEventListener('change', function () {
                state.page = 1;
                loadSearch();
            });
        });

        document.querySelectorAll('.search-chip').forEach(function (chip) {
            chip.style.cursor = 'pointer';
            chip.addEventListener('click', function () {
                var t = chip.getAttribute('data-type') || 'all';
                if (el('search-type')) el('search-type').value = t;
                state.page = 1;
                loadSearch();
            });
        });

        document.querySelectorAll('th.sortable').forEach(function (th) {
            th.style.cursor = 'pointer';
            th.addEventListener('click', function () {
                var sort = th.getAttribute('data-sort');
                var sortBy = el('search-sort-by');
                var sortDir = el('search-sort-dir');
                if (!sortBy || !sortDir) return;
                if (sortBy.value === sort) {
                    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc';
                } else {
                    sortBy.value = sort;
                    sortDir.value = 'asc';
                }
                state.page = 1;
                loadSearch();
            });
        });

        // Initial load (supports ?q= from navbar)
        loadSearch();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();
