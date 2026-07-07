/* ===========================================
   MyTube Sync - bootstrap / page integration
   =========================================== */

(function () {
  "use strict";

  function ready(fn) {
    if (document.body) fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  let historyTimer = null;
  let trackedVideoId = null;
  let trackedVideoEl = null;

  // YouTube serves a responsive www.youtube.com to most mobile browsers
  // nowadays (m.youtube.com is no longer guaranteed), so we can't rely on
  // hostname alone. Match it against the same breakpoint our CSS uses.
  function isMobileSite() {
    if (location.hostname === "m.youtube.com") return true;
    return window.matchMedia("(max-width: 900px), (pointer: coarse)").matches;
  }

  // ---------- Sidebar collapse to icons-only ----------
  // YouTube toggles a `guide-state` attribute on <ytd-app> and stores a
  // preference; clicking the native "guide" (hamburger) button toggles
  // between full / mini / hidden. We aim for the mini ("collapsed") state.
  function collapseGuideToMini() {
    const ytdApp = document.querySelector("ytd-app");
    if (!ytdApp) return;

    // If the mini-guide isn't being shown, click the masthead guide button
    // once to collapse the full guide drawer into icon-only mode.
    const guideDrawer = document.querySelector("tp-yt-app-drawer#guide");
    const isGuideVisible = guideDrawer && guideDrawer.hasAttribute("opened");

    if (isGuideVisible) {
      const guideButton = document.querySelector("ytd-masthead #guide-button button, ytd-masthead #guide-button");
      if (guideButton) guideButton.click();
    }
  }

  // ---------- YouTube's own mobile bottom nav offset ----------
  // YouTube's responsive web layout renders its own fixed bottom nav bar
  // (Home/Shorts/Subscriptions/You). We must stack our bar above it rather
  // than assuming bottom:0, or the two overlap. The exact element varies
  // by YouTube's current build, so we try a few known candidates and fall
  // back to 0 if none are found (our bar then sits at the true bottom).
  const YT_BOTTOM_NAV_SELECTORS = [
    "ytm-bottom-bar-layout",          // current YouTube mobile (2024+)
    "ytm-pivot-bar-renderer",         // older YouTube mobile
    "tp-yt-app-toolbar.bottom-bar",
    "#bottom-bar",
    "ytm-mobile-topbar-renderer + div[role='navigation']"
  ];

  function getYtBottomNavHeight() {
    for (const sel of YT_BOTTOM_NAV_SELECTORS) {
      const el = document.querySelector(sel);
      if (el) {
        const rect = el.getBoundingClientRect();
        // Only count it if it's actually pinned to the bottom of the viewport
        // and visible (avoids matching hidden/offscreen elements).
        if (rect.height > 0 && rect.bottom >= window.innerHeight - 2) {
          return Math.round(rect.height);
        }
      }
    }

    // Generic fallback: scan all fixed-position elements anchored to the
    // bottom and pick the tallest one that spans the full width and sits at
    // the true bottom. This catches future YouTube DOM changes automatically.
    let best = 0;
    for (const el of document.querySelectorAll("*")) {
      try {
        const style = window.getComputedStyle(el);
        if (style.position !== "fixed") continue;
        if (style.bottom !== "0px") continue;
        const rect = el.getBoundingClientRect();
        if (rect.height > 0 && rect.height < 120 &&
            rect.width >= window.innerWidth * 0.8 &&
            rect.bottom >= window.innerHeight - 2) {
          best = Math.max(best, Math.round(rect.height));
        }
      } catch (_) { /* skip shadow-dom elements that throw */ }
    }
    return best;
  }

  function syncYtBottomNavOffset() {
    const height = getYtBottomNavHeight();
    // Only write the var once we have a real measurement; the CSS default
    // keeps the bar safe until then.
    if (height > 0) {
      document.documentElement.style.setProperty("--mts-yt-bottom-nav-height", `${height}px`);
    }
  }

  // On /watch pages YouTube hides its own bottom nav — our bar should sit at
  // the true bottom (0). On all other pages we use a safe 56px default while
  // waiting for the nav to render, then refine with the real measurement.
  function getNavDefault() {
    return location.pathname.startsWith("/watch") ? "0px" : "56px";
  }

  // Poll until YouTube's nav bar exists, then measure and stop polling.
  // Uses MutationObserver so it fires the instant the element appears,
  // even on slow devices where YouTube's SPA takes >5s to render the nav.
  function waitForYtNavAndSync() {
    // Set a safe page-type default immediately
    document.documentElement.style.setProperty("--mts-yt-bottom-nav-height", getNavDefault());

    const height = getYtBottomNavHeight();
    if (height > 0) {
      document.documentElement.style.setProperty("--mts-yt-bottom-nav-height", `${height}px`);
      return;
    }

    // Watch the DOM for the nav element to appear
    const obs = new MutationObserver(() => {
      const h = getYtBottomNavHeight();
      if (h > 0) {
        document.documentElement.style.setProperty("--mts-yt-bottom-nav-height", `${h}px`);
        obs.disconnect();
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });

    // Safety: disconnect after 30s regardless
    setTimeout(() => obs.disconnect(), 30000);
  }

  // ---------- Shorts handling ----------
  // /shorts/ is a full-bleed vertical video; our floating mobile bars would
  // cover part of it, so we hide them entirely while on a Shorts page.
  function updateShortsState() {
    const isShorts = location.pathname.startsWith("/shorts/");
    document.documentElement.setAttribute("mts-shorts", isShorts ? "1" : "0");
    if (isShorts) {
      window.__mts.closeMobilePopup();
    }
  }

  // ---------- Fullscreen handling ----------
  function updateFullscreenState() {
    const fs = window.__mts.isFullscreenActive();
    document.documentElement.setAttribute("mts-fullscreen", fs ? "1" : "0");
  }

  // ---------- SPA navigation handling ----------
  let lastHref = location.href;

  function onNavigate() {
    if (location.href === lastHref) return;
    lastHref = location.href;

    detachVideoTracking();
    window.__mts.renderCurrentCard();
    // Re-render again shortly after: on /channel/UCxxxx pages YouTube sets
    // the canonical @handle link asynchronously after navigation.
    setTimeout(() => window.__mts.renderCurrentCard(), 1000);

    syncYtBottomNavOffset();
    setTimeout(waitForYtNavAndSync, 300);

    updateShortsState();

    if (!isMobileSite()) collapseGuideToMini();

    // Re-attach to the player after navigation (player element may be re-used or re-created)
    setTimeout(attachVideoTracking, 800);
  }

  // YouTube fires this custom event on SPA navigations
  window.addEventListener("yt-navigate-finish", onNavigate);

  // Fallback observer in case the event above isn't available (e.g. some mobile builds)
  const navObserver = new MutationObserver(() => onNavigate());
  navObserver.observe(document.body, { childList: true, subtree: true });

  // ---------- Video playback tracking ----------
  function findVideoElement() {
    return document.querySelector("video.html5-main-video, video");
  }

  function attachVideoTracking() {
    const videoId = window.__mts.getCurrentVideoId();
    if (!videoId) return;

    const videoEl = findVideoElement();
    if (!videoEl) {
      // Player might not be ready yet - retry shortly
      setTimeout(attachVideoTracking, 1000);
      return;
    }

    if (trackedVideoEl === videoEl && trackedVideoId === videoId) return;

    detachVideoTracking();

    trackedVideoId = videoId;
    trackedVideoEl = videoEl;

    // Immediate update on (re)attach
    sendHistoryUpdate();

    historyTimer = setInterval(sendHistoryUpdate, window.__mts.HISTORY_INTERVAL_MS);

    videoEl.addEventListener("pause", sendHistoryUpdate);
    videoEl.addEventListener("ended", sendCompletedUpdate);
    videoEl.addEventListener("seeked", sendHistoryUpdate);
  }

  function detachVideoTracking() {
    if (historyTimer) {
      clearInterval(historyTimer);
      historyTimer = null;
    }
    if (trackedVideoEl) {
      trackedVideoEl.removeEventListener("pause", sendHistoryUpdate);
      trackedVideoEl.removeEventListener("ended", sendCompletedUpdate);
      trackedVideoEl.removeEventListener("seeked", sendHistoryUpdate);
    }
    // Send a final update for the video we're leaving
    if (trackedVideoId && trackedVideoEl) {
      sendHistoryUpdate();
    }
    trackedVideoId = null;
    trackedVideoEl = null;
  }

  function sendHistoryUpdate() {
    if (!trackedVideoEl || !trackedVideoId) return;
    if (!window.__mts.appState.backendUrl || !window.__mts.appState.apiToken) return;

    const progress = Math.floor(trackedVideoEl.currentTime || 0);
    const duration = trackedVideoEl.duration || 0;
    const completed = duration > 0 && progress >= duration - 2;

    window.__mts.updateHistoryState(trackedVideoId, progress, completed);
  }

  function sendCompletedUpdate() {
    if (!trackedVideoEl || !trackedVideoId) return;
    const progress = Math.floor(trackedVideoEl.currentTime || 0);
    window.__mts.updateHistoryState(trackedVideoId, progress, true);
  }

  // ---------- Visibility / unload: flush final progress ----------
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") sendHistoryUpdate();
  });
  window.addEventListener("beforeunload", () => {
    sendHistoryUpdate();
  });

  // ---------- Storage change listener (popup updates) ----------
  browser.storage.onChanged.addListener(async (changes, area) => {
    if (area !== "local") return;
    if (changes.backendUrl || changes.apiToken || changes.userName) {
      await window.__mts.loadSettings();
      window.__mts.updateSyncIndicators();
      await window.__mts.syncAllState();
    }
    // Language changed from popup — rebuild UI with new strings
    if (changes.lang) {
      await window.__mts.initI18n();
      // Rebuild nav tab labels (icon span is untouched; only the label text updates)
      document.querySelectorAll(".mts-tab, .mts-mobile-tab").forEach(el => {
        const tabId = el.dataset.tab;
        if (!tabId || tabId === "user") return;
        const labelMap = { subs: "tabSubscriptions", playlists: "tabPlaylists", history: "tabHistory" };
        const key = labelMap[tabId];
        const labelEl = el.querySelector(".mts-tab-label");
        if (key && labelEl) labelEl.textContent = window.__mts.t(key);
      });
      // Re-apply active class to force a layout repaint on all tabs
      document.querySelectorAll("#mts-tabs .mts-tab").forEach(t => {
        t.classList.toggle("mts-tab-active", t.dataset.tab === window.__mts.getActiveTabId());
      });
      window.__mts.updateSyncIndicators();
      window.__mts.renderActiveTab();
      window.__mts.renderCurrentCard();
    }
  });

  // ---------- Init ----------
  ready(async () => {
    // i18n must resolve before any UI is built so all labels render in
    // the correct language from the first paint.
    await window.__mts.initI18n();

    await window.__mts.loadSettings();

    // Build both layouts; CSS media queries decide which is visible.
    // This avoids guessing "mobile vs desktop" once at load time, which
    // breaks for resizable windows and for mobile browsers serving the
    // responsive www.youtube.com instead of m.youtube.com.
    window.__mts.buildSidebar();
    window.__mts.buildMobileBar();

    document.documentElement.setAttribute("mts-active", "1");

    document.addEventListener("fullscreenchange", updateFullscreenState);
    document.addEventListener("webkitfullscreenchange", updateFullscreenState);
    updateFullscreenState();

    if (!isMobileSite()) {
      // Wait for YouTube's app shell to be ready before collapsing the guide
      setTimeout(collapseGuideToMini, 1500);
    }

    window.__mts.updateSyncIndicators();
    await window.__mts.syncAllState();

    // Set nav offset: waitForYtNavAndSync sets the page-type default immediately,
    // then polls until the real nav height is measurable. On resize the nav
    // already exists so the simpler syncYtBottomNavOffset is fine.
    waitForYtNavAndSync();
    window.addEventListener("resize", syncYtBottomNavOffset);

    updateShortsState();

    setTimeout(attachVideoTracking, 1000);

    // Periodic background resync (every 5 minutes) to pick up feed updates
    setInterval(() => window.__mts.syncAllState(), 5 * 60 * 1000);
  });
})();
