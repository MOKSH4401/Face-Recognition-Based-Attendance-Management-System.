/**
 * Admin / Faculty analytics dashboard — Chart.js + optional admin feed.
 */
(function () {
    'use strict';

    const API_URL = '/api/analytics';
    const charts = {};
    const chartDefaults = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {
                labels: { color: '#adb5bd' }
            }
        },
        scales: {
            x: {
                ticks: { color: '#adb5bd' },
                grid: { color: 'rgba(255,255,255,0.06)' }
            },
            y: {
                beginAtZero: true,
                ticks: { color: '#adb5bd', precision: 0 },
                grid: { color: 'rgba(255,255,255,0.06)' }
            }
        }
    };

    function setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function showError(message) {
        const box = document.getElementById('analytics-error');
        if (!box) return;
        box.textContent = message;
        box.classList.remove('d-none');
    }

    function clearError() {
        const box = document.getElementById('analytics-error');
        if (!box) return;
        box.classList.add('d-none');
        box.textContent = '';
    }

    function updateKPIs(summary) {
        setText('kpi-total-students', summary.total_students);
        setText('kpi-total-faculty', summary.total_faculty);
        setText('kpi-total-courses', summary.total_courses);
        setText('kpi-today-attendance', summary.today_attendance);
        setText('kpi-present', summary.present_students);
        setText('kpi-absent', summary.absent_students);
        setText('kpi-percentage', summary.attendance_percentage + '%');
        setText('kpi-weekly', summary.weekly_attendance);
        setText('kpi-monthly', summary.monthly_attendance);
    }

    function upsertChart(key, canvasId, config) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        if (charts[key]) {
            charts[key].data = config.data;
            charts[key].options = config.options;
            charts[key].update();
            return;
        }
        charts[key] = new Chart(canvas.getContext('2d'), config);
    }

    function renderCharts(chartData) {
        if (!chartData) return;

        upsertChart('weekly', 'chart-weekly-bar', {
            type: 'bar',
            data: {
                labels: chartData.weekly.labels,
                datasets: [{
                    label: 'Attendance marks',
                    data: chartData.weekly.values,
                    backgroundColor: 'rgba(13, 110, 253, 0.75)',
                    borderColor: 'rgba(13, 110, 253, 1)',
                    borderWidth: 1,
                    borderRadius: 8,
                    maxBarThickness: 48
                }]
            },
            options: Object.assign({}, chartDefaults)
        });

        upsertChart('pie', 'chart-present-pie', {
            type: 'doughnut',
            data: {
                labels: chartData.present_absent.labels,
                datasets: [{
                    data: chartData.present_absent.values,
                    backgroundColor: [
                        'rgba(25, 135, 84, 0.85)',
                        'rgba(220, 53, 69, 0.85)'
                    ],
                    borderWidth: 0,
                    hoverOffset: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '62%',
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: '#adb5bd', padding: 16 }
                    }
                }
            }
        });

        upsertChart('monthly', 'chart-monthly-line', {
            type: 'line',
            data: {
                labels: chartData.monthly.labels,
                datasets: [{
                    label: 'Monthly attendance',
                    data: chartData.monthly.values,
                    borderColor: 'rgba(25, 135, 84, 1)',
                    backgroundColor: 'rgba(25, 135, 84, 0.15)',
                    fill: true,
                    tension: 0.35,
                    pointRadius: 4,
                    pointBackgroundColor: 'rgba(25, 135, 84, 1)'
                }]
            },
            options: Object.assign({}, chartDefaults)
        });

        upsertChart('course', 'chart-course-bar', {
            type: 'bar',
            data: {
                labels: chartData.by_course.labels.length
                    ? chartData.by_course.labels
                    : ['No courses'],
                datasets: [{
                    label: 'Last 30 days',
                    data: chartData.by_course.values.length
                        ? chartData.by_course.values
                        : [0],
                    backgroundColor: 'rgba(255, 193, 7, 0.75)',
                    borderColor: 'rgba(255, 193, 7, 1)',
                    borderWidth: 1,
                    borderRadius: 8,
                    maxBarThickness: 40
                }]
            },
            options: Object.assign({}, chartDefaults, {
                indexAxis: chartData.by_course.labels.length > 6 ? 'y' : 'x'
            })
        });
    }

    function emptyList(msg) {
        return '<div class="list-group-item text-muted text-center py-4">' + escapeHtml(msg) + '</div>';
    }

    function renderRecentAttendance(items) {
        const el = document.getElementById('feed-recent-attendance');
        if (!el) return;
        if (!items || !items.length) {
            el.innerHTML = emptyList('No attendance records yet.');
            return;
        }
        el.innerHTML = items.map(function (a) {
            return (
                '<div class="list-group-item list-group-item-action bg-transparent border-secondary-subtle">' +
                '<div class="d-flex justify-content-between gap-2">' +
                '<div><div class="fw-semibold">' + escapeHtml(a.student_name) + '</div>' +
                '<small class="text-muted">' + escapeHtml(a.course_name) + '</small></div>' +
                '<div class="text-end"><div class="small">' + escapeHtml(a.date) + '</div>' +
                '<small class="text-muted">' + escapeHtml(a.time) + '</small></div>' +
                '</div></div>'
            );
        }).join('');
    }

    function renderRecentStudents(items) {
        const el = document.getElementById('feed-recent-students');
        if (!el) return;
        if (!items || !items.length) {
            el.innerHTML = emptyList('No students yet.');
            return;
        }
        el.innerHTML = items.map(function (s) {
            const courses = (s.courses != null)
                ? '<span class="badge text-bg-secondary">' + s.courses + ' course(s)</span>'
                : '';
            return (
                '<div class="list-group-item bg-transparent border-secondary-subtle">' +
                '<div class="d-flex justify-content-between align-items-center gap-2">' +
                '<div><div class="fw-semibold">' + escapeHtml(s.name) + '</div>' +
                '<small class="text-muted">' + escapeHtml(s.student_id) +
                (s.created_at ? ' · ' + escapeHtml(s.created_at) : '') + '</small></div>' +
                courses + '</div></div>'
            );
        }).join('');
    }

    function renderRecentFaculty(items) {
        const el = document.getElementById('feed-recent-faculty');
        if (!el) return;
        if (!items || !items.length) {
            el.innerHTML = emptyList('No faculty yet.');
            return;
        }
        el.innerHTML = items.map(function (f) {
            return (
                '<div class="list-group-item bg-transparent border-secondary-subtle">' +
                '<div class="fw-semibold">' + escapeHtml(f.name) + '</div>' +
                '<small class="text-muted">' + escapeHtml(f.designation) +
                ' · ' + escapeHtml(f.department) + '</small>' +
                (f.created_at ? '<div><small class="text-muted">' + escapeHtml(f.created_at) + '</small></div>' : '') +
                '</div>'
            );
        }).join('');
    }

    function renderActivities(items) {
        const el = document.getElementById('feed-activities');
        if (!el) return;
        if (!items || !items.length) {
            el.innerHTML = '<div class="text-muted text-center py-4">No recent activity.</div>';
            return;
        }
        el.innerHTML = '<ul class="list-unstyled activity-timeline mb-0 px-3 py-2">' + items.map(function (a) {
            return (
                '<li class="activity-item d-flex gap-3 py-3 border-bottom border-secondary-subtle">' +
                '<div class="activity-icon text-' + escapeHtml(a.color || 'primary') + '">' +
                '<i class="fas ' + escapeHtml(a.icon || 'fa-circle') + '"></i></div>' +
                '<div class="flex-grow-1">' +
                '<div class="fw-semibold">' + escapeHtml(a.title) + '</div>' +
                '<small class="text-muted">' + escapeHtml(a.meta || '') +
                (a.at ? ' · ' + escapeHtml(a.at) : '') + '</small>' +
                '</div></li>'
            );
        }).join('') + '</ul>';
    }

    function renderFeed(feed) {
        if (!feed) return;
        renderRecentAttendance(feed.recent_attendance);
        renderRecentStudents(feed.recent_students);
        renderRecentFaculty(feed.recent_faculty);
        renderActivities(feed.activities);
    }

    function loadAnalytics() {
        clearError();
        const btn = document.getElementById('btn-refresh-analytics');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Loading';
        }

        fetch(API_URL, { credentials: 'same-origin' })
            .then(function (res) {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(function (payload) {
                if (payload.status !== 'success') {
                    throw new Error(payload.message || 'Failed to load analytics');
                }
                updateKPIs(payload.data.summary || {});
                renderCharts(payload.data.charts);
                renderFeed(payload.data.feed);
                const updated = document.getElementById('analytics-updated');
                if (updated && payload.data.generated_at) {
                    updated.textContent = '· Updated ' + new Date(payload.data.generated_at).toLocaleString();
                }
            })
            .catch(function (err) {
                console.error(err);
                showError('Could not load dashboard data. Please try refreshing.');
            })
            .finally(function () {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-sync-alt me-1"></i>Refresh';
                }
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!document.getElementById('chart-weekly-bar')) return;
        loadAnalytics();
        const btn = document.getElementById('btn-refresh-analytics');
        if (btn) btn.addEventListener('click', loadAnalytics);
        setInterval(loadAnalytics, 60000);
    });
})();
