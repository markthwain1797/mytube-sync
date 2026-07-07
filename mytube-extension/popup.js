/* Embedded translations for the popup.
   Must stay in sync with _locales/[lang]/messages.json. */
const POPUP_STRINGS = {
  en: {
    lblUrl:        "Backend URL",
    lblToken:      "API Token",
    lblLang:       "Language",
    optAuto:       "Auto (browser default)",
    btnConnect:    "Connect",
    noUrl:         "Enter a backend URL.",
    noToken:       "Enter an API token.",
    connectOk:     name => `Connected as "${name}".`,
    connectFail:   err  => `Connection failed: ${err}`
  },
  de: {
    lblUrl:        "Backend-URL",
    lblToken:      "API-Token",
    lblLang:       "Sprache",
    optAuto:       "Automatisch (Browsersprache)",
    btnConnect:    "Verbinden",
    noUrl:         "Bitte Backend-URL eingeben.",
    noToken:       "Bitte API-Token eingeben.",
    connectOk:     name => `Verbunden als „${name}"`,
    connectFail:   err  => `Verbindung fehlgeschlagen: ${err}`
  }
};

function detectLang(override) {
  if (override && POPUP_STRINGS[override]) return override;
  const lang = (browser.i18n.getUILanguage() || "en").toLowerCase();
  return lang.startsWith("de") ? "de" : "en";
}

let strings = POPUP_STRINGS.en;

function applyStrings(lang) {
  strings = POPUP_STRINGS[lang] || POPUP_STRINGS.en;
  document.getElementById("lbl-url").textContent    = strings.lblUrl;
  document.getElementById("lbl-token").textContent  = strings.lblToken;
  document.getElementById("lbl-lang").textContent   = strings.lblLang;
  document.getElementById("saveBtn").textContent    = strings.btnConnect;
  const autoOpt = document.querySelector('#langSelect option[value=""]');
  if (autoOpt) autoOpt.textContent = strings.optAuto;
}

function showStatus(msg, isError) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = isError ? "err" : "ok";
}

async function init() {
  const stored = await browser.storage.local.get(["backendUrl", "apiToken", "userName", "lang"]);

  const langOverride = stored.lang || "";
  const activeLang = detectLang(langOverride);
  applyStrings(activeLang);

  if (stored.backendUrl) document.getElementById("backendUrl").value = stored.backendUrl;
  if (stored.apiToken)   document.getElementById("apiToken").value   = stored.apiToken;
  document.getElementById("langSelect").value = langOverride;

  if (stored.userName) {
    document.getElementById("currentInfo").textContent = strings.connectOk(stored.userName);
  }
}

let saveDebounce = null;
function debouncedSaveInputs() {
  clearTimeout(saveDebounce);
  saveDebounce = setTimeout(async () => {
    const url   = document.getElementById("backendUrl").value.trim();
    const token = document.getElementById("apiToken").value.trim();
    if (url)   await browser.storage.local.set({ backendUrl: url });
    if (token) await browser.storage.local.set({ apiToken: token });
  }, 400);
}

document.getElementById("backendUrl").addEventListener("input", debouncedSaveInputs);
document.getElementById("apiToken").addEventListener("input", debouncedSaveInputs);

document.getElementById("langSelect").addEventListener("change", async () => {
  const val = document.getElementById("langSelect").value;
  await browser.storage.local.set({ lang: val || "" });
  const activeLang = detectLang(val);
  applyStrings(activeLang);
  // Update currentInfo label language if already connected
  const stored = await browser.storage.local.get(["userName"]);
  if (stored.userName) {
    document.getElementById("currentInfo").textContent = strings.connectOk(stored.userName);
  }
});

document.getElementById("saveBtn").addEventListener("click", async () => {
  const backendUrl = document.getElementById("backendUrl").value.trim().replace(/\/+$/, "");
  const token      = document.getElementById("apiToken").value.trim();

  if (!backendUrl) return showStatus(strings.noUrl, true);
  if (!token)      return showStatus(strings.noToken, true);

  try {
    const res = await fetch(`${backendUrl}/current_user`, {
      headers: { "Authorization": `Bearer ${token}` }
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const user = await res.json();

    await browser.storage.local.set({ backendUrl, apiToken: token, userName: user.name });
    document.getElementById("currentInfo").textContent = strings.connectOk(user.name);
    showStatus(strings.connectOk(user.name), false);
  } catch (e) {
    showStatus(strings.connectFail(e.message), true);
  }
});

init();
