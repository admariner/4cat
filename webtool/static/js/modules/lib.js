/**
 * Get absolute API URL to call
 *
 * Determines proper URL to call
 *
 * @param endpoint Relative URL to call (/api/endpoint)
 * @returns  Absolute URL
 */
export function getRelativeURL(endpoint) {
    let root = $("body").attr("data-url-root");
    if (!root) {
        root = '/';
    }
    return root + endpoint;
}

export function applyProgress(element, progress) {
    if (element.parent().hasClass('button-like')) {
        element = element.parent();
    }

    let current_progress = Array(...element[0].classList).filter(z => z.indexOf('progress-') === 0);
    for (let class_name in current_progress) {
        class_name = current_progress[class_name];
        element.removeClass(class_name);
    }

    if (progress && progress > 0 && progress < 100) {
        element.addClass('progress-' + progress);
        if (!element.hasClass('progress')) {
            element.addClass('progress');
        }
    }
}

/**
 * Return a FileReader, but as a Promise that can be awaited
 *
 * @param file
 * @returns {Promise<unknown>}
 * @constructor
 */
export function FileReaderPromise(file) {
    return new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onerror = reject;
        fr.onload = () => {
            resolve(fr.result);
        };
        fr.readAsText(file);
    });
}

/**
 * Convert HSV colour to HSL
 *
 * Expects a {0-360}, {0-100}, {0-100} value.
 *
 * @param h
 * @param s
 * @param v
 * @returns {String}
 */
export function hsv2hsl(h, s, v) {
    s /= 100;
    v /= 100;
    const vmin = Math.max(v, 0.01);
    let sl;
    let l;

    l = (2 - s) * v;
    const lmin = (2 - s) * vmin;
    sl = s * vmin;
    sl /= (lmin <= 1) ? lmin : 2 - lmin;
    sl = sl || 0;
    l /= 2;

    return 'hsl(' + h + 'deg, ' + (sl * 100) + '%, ' + (l * 100) + '%)';
}

export function find_parent(element, selector, start_self = false) {
    while (element.parentNode) {
        if (!start_self) {
            element = element.parentNode;
        }
        if (element instanceof HTMLDocument) {
            return null;
        }
        if (element.matches(selector)) {
            return element;
        }
        if (start_self) {
            element = element.parentNode;
        }
    }

    return null;
}

