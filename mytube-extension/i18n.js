/* ===========================================
   MyTube Sync - i18n helper
   =========================================== */

(function () {
  "use strict";

  const SUPPORTED = ["en", "de"];
  const DEFAULT_LOCALE = "en";

  let activeMessages = null;
  let activeLang = null;

  function browserLang() {
    const lang = (navigator.language || "en").toLowerCase().split("-")[0];
    return SUPPORTED.includes(lang) ? lang : DEFAULT_LOCALE;
  }

  async function loadLocale(lang) {
    try {
      const url = browser.runtime.getURL(`_locales/${lang}/messages.json`);
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      console.warn(`MyTube i18n: failed to load locale "${lang}"`, e);
      return null;
    }
  }

  // Call once at startup. Reads stored lang preference,
  // falls back to browser language.
  async function initI18n() {
    const stored = await browser.storage.local.get("lang");
    const preferred = stored.lang || browserLang();
    activeLang = SUPPORTED.includes(preferred) ? preferred : DEFAULT_LOCALE;

    // Always load messages manually so we're not at the mercy of browser.i18n's
    // own locale detection (which can't be overridden at runtime).
    activeMessages = await loadLocale(activeLang);
    if (!activeMessages) {
      // Fallback: load English
      activeMessages = await loadLocale(DEFAULT_LOCALE);
    }
  }

  // Main translation function.
  function t(key, ...substitutions) {
    if (activeMessages) {
      const entry = activeMessages[key];
      if (!entry) return key;

      let msg = entry.message;

      if (entry.placeholders) {
        for (const [name, def] of Object.entries(entry.placeholders)) {
          const idx = parseInt(def.content.replace("$", ""), 10) - 1;
          const value = substitutions[idx] ?? "";
          msg = msg.replace(new RegExp(`\\$${name.toUpperCase()}\\$`, "gi"), value);
        }
      }

      return msg;
    }

    try {
      const result = browser.i18n.getMessage(key, substitutions);
      return result || key;
    } catch (e) {
      return key;
    }
  }

  function getCurrentLang() {
    return activeLang || browserLang();
  }

  async function setLang(lang) {
    if (!SUPPORTED.includes(lang)) return;
    await browser.storage.local.set({ lang });
    activeLang = lang;
    activeMessages = await loadLocale(lang) || await loadLocale(DEFAULT_LOCALE);
  }

  window.__mtsi18n = { initI18n, t, getCurrentLang, setLang, SUPPORTED };
})();
