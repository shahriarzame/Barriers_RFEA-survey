(function () {
    'use strict';
    function setup() {
        if (typeof jQuery === 'undefined') { setTimeout(setup, 100); return; }
        var $ = jQuery;
        $('[class*="bws-most-"], [class*="bws-least-"]').each(function () {
            var m = this.className.match(/(?:^|\s)bws-(?:most|least)-([A-Za-z0-9]+)/);
            if (!m) return;
            var sel = '.bws-cmp-' + m[1] + ' input[type="radio"]';
            $(this).find('input[type="radio"]').on('change.bwsreset', function () {
                $(sel).prop('checked', false);
            });
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', setup);
    } else { setup(); }
})();
