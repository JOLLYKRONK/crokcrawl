"""Stealth mode for Playwright — masks automation fingerprints to bypass bot detection.

Inspired by playwright-stealth and undetected-playwright. Handles Cloudflare,
PerimeterX, and other WAF bot-detection challenges.
"""

STEALTH_INIT_SCRIPT = """
// Override webdriver detection
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Mock chrome runtime (detected by some anti-bot scripts)
window.chrome = {
    runtime: {},
    loadTimes: function() { return {}; },
    csi: function() { return {}; },
    app: {
        isInstalled: false,
    },
};

// Mock navigator plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojoftjoefpfjbn'},
        {name: 'Native Client', filename: 'internal-nacl-plugin'},
    ],
});

// Mock navigator languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

// Remove Playwright automation markers
delete globalThis.__playwright;

// Override permissions query
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters);

// Spoil iframe contentWindow detection
Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    get: function() {
        return window;
    },
});

// Spoil iframe contentDocument
Object.defineProperty(HTMLIFrameElement.prototype, 'contentDocument', {
    get: function() {
        return document;
    },
});

// Override navigator connection (hardware info leaks)
Object.defineProperty(navigator, 'connection', {
    get: () => undefined,
});

// Override navigator hardwareConcurrency (common fingerprint)
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
});

// Override navigator deviceMemory
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
});

// Spoil toString for various prototypes
const originalToString = Function.prototype.toString;
Function.prototype.toString = function() {
    if (this === navigator.webdriver.get || this === Object.defineProperty) {
        return originalToString.call(Object);
    }
    return originalToString.call(this);
};

// Override the evaluation of document.documentElement for automation detection
const rawSetAttribute = HTMLElement.prototype.setAttribute;
HTMLElement.prototype.setAttribute = function(name, value) {
    if (name === 'webdriver' || name === 'data-driver') {
        return;
    }
    rawSetAttribute.call(this, name, value);
};
"""

# Additional browser args to disable automation markers
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-web-security",
    "--disable-features=VizDisplayCompositor",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--metrics-recording-only",
    "--no-first-run",
    "--safebrowsing-disable-auto-update",
]
