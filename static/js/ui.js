/**
 * UI shell: dark mode toggle + responsive sidebar + subtle reveals.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'ams-theme';

    function currentTheme() {
        return document.documentElement.getAttribute('data-bs-theme') || 'dark';
    }

    function syncThemeIcons(theme) {
        document.querySelectorAll('.theme-icon-dark').forEach(function (el) {
            el.classList.toggle('d-none', theme === 'light');
        });
        document.querySelectorAll('.theme-icon-light').forEach(function (el) {
            el.classList.toggle('d-none', theme === 'dark');
        });
    }

    function setTheme(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
        try {
            localStorage.setItem(STORAGE_KEY, theme);
        } catch (e) {}
        syncThemeIcons(theme);
    }

    function toggleTheme() {
        setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
    }

    function openSidebar() {
        document.body.classList.add('sidebar-open');
        var backdrop = document.getElementById('sidebar-backdrop');
        if (backdrop) backdrop.setAttribute('aria-hidden', 'false');
    }

    function closeSidebar() {
        document.body.classList.remove('sidebar-open');
        var backdrop = document.getElementById('sidebar-backdrop');
        if (backdrop) backdrop.setAttribute('aria-hidden', 'true');
    }

    function enhanceCards() {
        var cards = document.querySelectorAll('.content-inner .card');
        cards.forEach(function (card, i) {
            if (card.style.animationDelay) return;
            card.style.animation = 'ams-fade-up 0.45s cubic-bezier(0.22, 1, 0.36, 1) both';
            card.style.animationDelay = Math.min(i * 0.03, 0.35) + 's';
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        syncThemeIcons(currentTheme());
        enhanceCards();

        document.querySelectorAll('#theme-toggle').forEach(function (btn) {
            btn.addEventListener('click', toggleTheme);
        });

        var toggle = document.getElementById('sidebar-toggle');
        var closeBtn = document.getElementById('sidebar-close');
        var backdrop = document.getElementById('sidebar-backdrop');
        if (toggle) toggle.addEventListener('click', openSidebar);
        if (closeBtn) closeBtn.addEventListener('click', closeSidebar);
        if (backdrop) backdrop.addEventListener('click', closeSidebar);

        window.addEventListener('resize', function () {
            if (window.innerWidth >= 992) closeSidebar();
        });

        document.querySelectorAll('.sidebar-nav .nav-link').forEach(function (link) {
            link.addEventListener('click', function () {
                if (window.innerWidth < 992) closeSidebar();
            });
        });
    });
})();
