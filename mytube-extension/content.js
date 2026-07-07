/* ===========================================
   MyTube Sync - content script
   =========================================== */

(function () {
  "use strict";

  window.__mts = window.__mts || {};

  // ---------- Constants ----------
  const SYS_LIKED = "Liked Videos";
  const SYS_LATER = "Watch Later";
  const META_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
  const HISTORY_INTERVAL_MS = 15000;

  // ---------- App State ----------
  const appState = {
    backendUrl: null,
    apiToken: null,
    userName: null,
    subscriptions: [],
    playlists: [],
    history: [],
    playlistContentsMap: {},
    backendFeed: [],
    syncStatus: "idle", // idle | busy | ok | err
    activeMobilePopup: null,
    expandedPlaylists: {}
  };

  let metadataCache = {};
  let metadataLoaded = false;

  // ---------- Storage ----------
  async function loadSettings() {
    const stored = await browser.storage.local.get(["backendUrl", "apiToken", "userName"]);
    appState.backendUrl = stored.backendUrl || null;
    appState.apiToken = stored.apiToken || null;
    appState.userName = stored.userName || null;
  }

  async function loadMetadataCache() {
    if (metadataLoaded) return;
    const stored = await browser.storage.local.get(["mts_metadata_cache"]);
    metadataCache = stored.mts_metadata_cache || {};
    metadataLoaded = true;

    // Prune expired entries
    const now = Date.now();
    let changed = false;
    for (const id of Object.keys(metadataCache)) {
      if (!metadataCache[id]._ts || (now - metadataCache[id]._ts) > META_TTL_MS) {
        delete metadataCache[id];
        changed = true;
      }
    }
    if (changed) saveMetadataCache();
  }

  let metaSaveTimer = null;
  function saveMetadataCache() {
    if (metaSaveTimer) clearTimeout(metaSaveTimer);
    metaSaveTimer = setTimeout(() => {
      browser.storage.local.set({ mts_metadata_cache: metadataCache });
    }, 500);
  }

  // ---------- API ----------
  function getHeaders() {
    return {
      "Authorization": `Bearer ${appState.apiToken}`,
      "Content-Type": "application/json"
    };
  }

  async function apiRequest(endpoint, method = "GET", body = null) {
    if (!appState.backendUrl || !appState.apiToken) {
      throw new Error("Not configured");
    }
    const config = { method, headers: getHeaders() };
    if (body) config.body = JSON.stringify(body);

    const response = await fetch(`${appState.backendUrl}${endpoint}`, config);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  }

  // ---------- Video metadata (oEmbed, cached) ----------
  async function getVideoMetadata(videoId) {
    await loadMetadataCache();

    const cached = metadataCache[videoId];
    if (cached && cached._ts && (Date.now() - cached._ts) < META_TTL_MS) {
      return cached;
    }

    try {
      const response = await fetch(
        `https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=${videoId}&format=json`
      );
      if (!response.ok) throw new Error("oembed failed");
      const data = await response.json();

      let extractedHandle = null;
      if (data.author_url) {
        const parts = data.author_url.split("/");
        const lastPart = parts[parts.length - 1];
        if (lastPart.startsWith("@")) extractedHandle = lastPart;
      }

      const meta = {
        title: data.title,
        author: data.author_name,
        handle: extractedHandle,
        thumbnail: data.thumbnail_url || `https://img.youtube.com/vi/${videoId}/mqdefault.jpg`,
        _ts: Date.now()
      };

      metadataCache[videoId] = meta;
      saveMetadataCache();
      return meta;
    } catch (e) {
      // Fallback - don't cache failures (so we retry later)
      return {
        title: `Video ${videoId}`,
        author: "Unknown",
        handle: null,
        thumbnail: `https://img.youtube.com/vi/${videoId}/mqdefault.jpg`,
        _ts: 0
      };
    }
  }

  function getPlaybackLink(videoId) {
    const historyMatch = appState.history.find(h => h.video_id === videoId);
    if (historyMatch && historyMatch.progress_seconds > 0 && !historyMatch.completed) {
      return `https://www.youtube.com/watch?v=${videoId}&t=${historyMatch.progress_seconds}s`;
    }
    return `https://www.youtube.com/watch?v=${videoId}`;
  }

  function renderProgressBadge(videoId) {
    const track = appState.history.find(h => h.video_id === videoId);
    if (!track) return "";
    if (track.completed) {
      return `<div class="mts-progress-badge mts-watched">${window.__mts.t("watched")}</div>`;
    }
    if (track.progress_seconds > 0) {
      return `<div class="mts-progress-badge">${window.__mts.t("resumeAt", formatTimestamp(track.progress_seconds))}</div>`;
    }
    return "";
  }

  function formatTimestamp(totalSeconds) {
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    if (h > 0) {
      return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  // ---------- Sync ----------
  let syncInFlight = false;

  // Prefetch metadata for a list of videoIds in parallel (up to `concur` at once)
  async function prefetchMetadata(ids, concur = 25) {
    await loadMetadataCache();
    const missing = [...new Set(ids)].filter(id => {
      const c = metadataCache[id];
      return !c || !c._ts || (Date.now() - c._ts) >= META_TTL_MS;
    });
    for (let i = 0; i < missing.length; i += concur) {
      await Promise.all(missing.slice(i, i + concur).map(getVideoMetadata));
    }
  }

  async function syncAllState() {
    if (!appState.backendUrl || !appState.apiToken) {
      appState.syncStatus = "err";
      updateSyncIndicators();
      return;
    }
    if (syncInFlight) return;
    syncInFlight = true;
    appState.syncStatus = "busy";
    updateSyncIndicators();

    try {
      // Need subs + playlists synchronously first only to ensure system playlists exist.
      // Everything else fires in parallel and renders as it arrives.
      let [subs, playlists] = await Promise.all([
        apiRequest("/subscriptions"),
        apiRequest("/playlists"),
      ]);

      // Ensure system playlists exist on first run
      const hasLiked = playlists.some(p => p.name === SYS_LIKED);
      const hasLater = playlists.some(p => p.name === SYS_LATER);
      if (!hasLiked || !hasLater) {
        if (!hasLiked) await apiRequest("/playlists", "POST", { name: SYS_LIKED });
        if (!hasLater) await apiRequest("/playlists", "POST", { name: SYS_LATER });
        playlists = await apiRequest("/playlists");
      }

      appState.subscriptions = subs;
      appState.playlists     = playlists;
      appState.history       = [];

      // Persist userName so the label survives browser restarts
      try {
        const user = await apiRequest("/current_user");
        appState.userName = user.name;
        browser.storage.local.set({ userName: user.name });
      } catch (_) {}

      // Render what we have immediately (subs list, empty history)
      appState.syncStatus = "ok";
      syncInFlight = false;
      updateSyncIndicators();
      renderActiveTab();
      renderCurrentCard();

      // ── All remaining fetches are fully parallel ──────────────

      // Playlist contents + oEmbed, then re-render
      Promise.all(playlists.map(async pl => {
        appState.playlistContentsMap[pl.id] = await apiRequest(`/playlists/${pl.id}`);
      })).then(async () => {
        const ids = Object.values(appState.playlistContentsMap).flat().map(i => i.video_id);
        await prefetchMetadata(ids);
        renderActiveTab();
      }).catch(() => {});

      // History — set and re-render when it arrives
      apiRequest("/history").then(history => {
        appState.history = history;
        renderActiveTab();
        renderCurrentCard();
      }).catch(() => {});

      // Feed — set and re-render when it arrives
      apiRequest("/subscriptions/feed").then(async feed => {
        appState.backendFeed = feed;
        const ids = feed.flatMap(ch => (ch.videos || []).map(v => v.video_id));
        await prefetchMetadata(ids);
        renderActiveTab();
      }).catch(() => {
        appState.backendFeed = [];
      });

    } catch (e) {
      console.error("MyTube Sync error:", e);
      appState.syncStatus = "err";
      syncInFlight = false;
      updateSyncIndicators();
      renderActiveTab();
      renderCurrentCard();
    }
  }

  function updateSyncIndicators() {
    const dots = document.querySelectorAll(".mts-sync-dot");
    dots.forEach(dot => {
      dot.classList.remove("ok", "err", "busy");
      if (appState.syncStatus === "ok") dot.classList.add("ok");
      else if (appState.syncStatus === "err") dot.classList.add("err");
      else if (appState.syncStatus === "busy") dot.classList.add("busy");
    });
    const userLabels = document.querySelectorAll(".mts-user-label");
    userLabels.forEach(el => {
      el.textContent = appState.userName || window.__mts.t("notConnected");
    });
    const reloadBtn = document.getElementById("mts-reload-btn");
    if (reloadBtn) {
      reloadBtn.classList.toggle("mts-spinning", appState.syncStatus === "busy");
    }
  }

  // ---------- Stateful mutations ----------
  async function togglePlaylistItem(videoId, playlistId, isCurrentlyIn) {
    if (isCurrentlyIn) {
      await apiRequest(`/playlists/${playlistId}/item/${videoId}`, "DELETE");
    } else {
      await apiRequest(`/playlists/${playlistId}/add`, "POST", { video_id: videoId });
    }
    await syncAllState();
  }

  async function deletePlaylistItem(playlistId, videoId) {
    await apiRequest(`/playlists/${playlistId}/item/${videoId}`, "DELETE");
    await syncAllState();
  }

  async function subscribeChannel(channelInput) {
    let clean = channelInput.trim();
    if (clean.includes("youtube.com/")) clean = clean.split("/").pop();
    await apiRequest("/subscriptions", "POST", { channel_id: clean });
    await syncAllState();
  }

  async function unsubscribeChannel(channelId) {
    await apiRequest(`/subscriptions/${channelId}`, "DELETE");
    await syncAllState();
  }

  async function updateHistoryState(videoId, progressSeconds, completed) {
    try {
      await apiRequest("/history/update", "POST", {
        video_id: videoId,
        progress_seconds: progressSeconds,
        completed: completed
      });
      const existing = appState.history.find(h => h.video_id === videoId);
      if (existing) {
        existing.progress_seconds = progressSeconds;
        existing.completed = completed;
      } else {
        appState.history.unshift({
          video_id: videoId,
          progress_seconds: progressSeconds,
          completed: completed,
          last_watched_at: new Date().toISOString()
        });
      }
    } catch (e) {
      console.error("History update failed:", e);
    }
  }

  // ---------- URL / page-state helpers ----------
  function getCurrentVideoId() {
    const url = new URL(location.href);
    if (url.pathname === "/watch") {
      return url.searchParams.get("v");
    }
    if (url.pathname.startsWith("/shorts/")) {
      return url.pathname.split("/")[2];
    }
    return null;
  }

  // Returns { handle, rawId } where handle is "@something" if resolvable,
  // and rawId is the raw "UCxxxx" id if the URL is a /channel/ page.
  function getCurrentChannelInfo() {
    const url = new URL(location.href);

    const handleMatch = url.pathname.match(/^\/(@[a-zA-Z0-9_.-]+)/);
    if (handleMatch) {
      return { handle: handleMatch[1], rawId: null };
    }

    const channelMatch = url.pathname.match(/^\/channel\/(UC[a-zA-Z0-9_-]+)/);
    if (channelMatch) {
      const rawId = channelMatch[1];
      // Try to resolve the @handle from the page's canonical link,
      // which YouTube sets to the channel's handle-based URL when one exists.
      const canonical = document.querySelector('link[rel="canonical"]');
      if (canonical && canonical.href) {
        const canonicalMatch = canonical.href.match(/\/(@[a-zA-Z0-9_.-]+)/);
        if (canonicalMatch) {
          return { handle: canonicalMatch[1], rawId };
        }
      }
      return { handle: null, rawId };
    }

    return { handle: null, rawId: null };
  }

  function isFullscreenActive() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  // ---------- UI: Scaffolding ----------
  // TABS is a function so labels are re-evaluated after initI18n resolves.
  function getTabs() {
    const { t } = window.__mts;
    return [
      { id: "subs",      label: t("tabSubscriptions"), icon: "📺" },
      { id: "playlists", label: t("tabPlaylists"),     icon: "📂" },
      { id: "history",   label: t("tabHistory"),       icon: "🕘" }
    ];
  }

  function buildSidebar() {
    if (document.getElementById("mts-sidebar")) return;
    const { t } = window.__mts;
    const tabs = getTabs();

    const sidebar = document.createElement("div");
    sidebar.id = "mts-sidebar";
    sidebar.innerHTML = `
      <div id="mts-header">
        <span><span class="mts-sync-dot"></span><span class="mts-user-label">${t("notConnected")}</span></span>
        <button id="mts-reload-btn" title="${t("reloadTitle")}">⟳</button>
      </div>
      <div id="mts-current-card" class="mts-empty"></div>
      <div id="mts-tabs">
        ${tabs.map(tab => `<div class="mts-tab" data-tab="${tab.id}"><span class="mts-icon">${tab.icon}</span><span class="mts-tab-label">${tab.label}</span></div>`).join("")}
      </div>
      <div id="mts-tab-content"></div>
    `;
    document.body.appendChild(sidebar);

    sidebar.querySelectorAll(".mts-tab").forEach(tab => {
      tab.addEventListener("click", () => setActiveTab(tab.dataset.tab));
    });

    sidebar.querySelector("#mts-reload-btn").addEventListener("click", () => syncAllState());

    setActiveTab("subs");
  }

  function buildMobileBar() {
    if (document.getElementById("mts-mobile-bar")) return;
    const { t } = window.__mts;
    const tabs = getTabs();

    const bar = document.createElement("div");
    bar.id = "mts-mobile-bar";
    bar.innerHTML = tabs.map(tab => `
      <div class="mts-mobile-tab" data-tab="${tab.id}">
        <span class="mts-icon">${tab.icon}</span><span class="mts-tab-label">${tab.label}</span>
      </div>
    `).join("") + `
      <div class="mts-mobile-tab" data-tab="user">
        <span class="mts-icon"><span class="mts-sync-dot"></span></span>
        <span class="mts-user-label">${t("notConnected")}</span>
      </div>
    `;
    document.body.appendChild(bar);

    const actions = document.createElement("div");
    actions.id = "mts-mobile-actions";
    actions.className = "mts-empty";
    document.body.appendChild(actions);

    const popup = document.createElement("div");
    popup.id = "mts-mobile-popup";
    document.body.appendChild(popup);

    bar.querySelectorAll(".mts-mobile-tab").forEach(tab => {
      tab.addEventListener("click", () => {
        const tabId = tab.dataset.tab;
        if (tabId === "user") {
          syncAllState();
          return;
        }

        if (appState.activeMobilePopup === tabId) {
          closeMobilePopup();
        } else {
          openMobilePopup(tabId);
        }
      });
    });
  }

  function openMobilePopup(tabId) {
    appState.activeMobilePopup = tabId;
    const popup = document.getElementById("mts-mobile-popup");
    popup.classList.add("mts-open");
    document.querySelectorAll(".mts-mobile-tab").forEach(t => {
      t.classList.toggle("mts-tab-active", t.dataset.tab === tabId);
    });
    setActiveTab(tabId, popup);
  }

  function closeMobilePopup() {
    appState.activeMobilePopup = null;
    const popup = document.getElementById("mts-mobile-popup");
    if (popup) {
      popup.classList.remove("mts-open");
      popup.innerHTML = "";
    }
    document.querySelectorAll(".mts-mobile-tab").forEach(t => t.classList.remove("mts-tab-active"));
  }

  // Desktop sidebar and the mobile popup are independent surfaces that can
  // each show a different tab at the same time (e.g. desktop left on
  // "Subscriptions" while mobile popup is opened to "Playlists"). They used
  // to share one global `activeTabId`, which caused content from whichever
  // tab was switched to *last* to be rendered into *both* surfaces on any
  // untargeted refresh (resync, playlist toggle, etc.) — this is what caused
  // history/playlist/sub content to bleed into the wrong tab.
  let desktopTabId = "subs";
  let mobileTabId = "subs";
  let _renderGen = 0; // incremented each render; used to abort stale async renders

  function setActiveTab(tabId, container) {
    if (container) {
      mobileTabId = tabId;
    } else {
      desktopTabId = tabId;
      document.querySelectorAll("#mts-tabs .mts-tab").forEach(t => {
        t.classList.toggle("mts-tab-active", t.dataset.tab === tabId);
      });
    }
    renderActiveTab(container);
  }

  async function renderActiveTab(container) {
    const gen = ++_renderGen;

    let target = container;
    let tabId;
    if (target) {
      tabId = mobileTabId;
    } else {
      // No explicit container: refresh the desktop sidebar...
      target = document.getElementById("mts-tab-content");
      tabId = desktopTabId;
      await renderInto(target, tabId, gen);

      // ...and ALSO refresh the mobile popup if it's currently open, using
      // its own independent tab state. Both surfaces can be live at once.
      const mobilePopup = document.getElementById("mts-mobile-popup");
      if (mobilePopup && mobilePopup.classList.contains("mts-open")) {
        await renderInto(mobilePopup, mobileTabId, gen);
      }
      return;
    }
    if (!target) return;

    await renderInto(target, tabId, gen);
  }

  async function renderInto(target, tabId, gen) {
    if (!target) return;

    // Reset scroll position so switching tabs always starts at the top
    target.scrollTop = 0;

    target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("notConnectedMsg")}</div>`;

    if (tabId === "subs") {
      await renderSubscriptionsTab(target, gen);
    } else if (tabId === "playlists") {
      await renderPlaylistsTab(target, gen);
    } else if (tabId === "history") {
      await renderHistoryTab(target, gen);
    }
  }

  // ---------- UI: Subscriptions Tab ----------
  async function renderSubscriptionsTab(target, gen) {
    if (!appState.backendUrl || !appState.apiToken) {
      target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("notConnectedMsg")}</div>`;
      return;
    }
    if (appState.backendFeed.length === 0) {
      // Show channel list while feed is loading, or empty state if no subs at all
      if (appState.subscriptions.length === 0) {
        target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("noSubscriptions")}</div>`;
      } else {
        target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("loading")}</div>`;
      }
      return;
    }

    // Flatten all videos across channels and sort by upload date (newest first)
    const allVideos = [];
    for (const feedItem of appState.backendFeed) {
      for (const v of (feedItem.videos || [])) {
        allVideos.push({ ...v, channel_id: feedItem.channel_id });
      }
    }

    allVideos.sort((a, b) => {
      const da = a.published ? new Date(a.published).getTime() : 0;
      const db = b.published ? new Date(b.published).getTime() : 0;
      return db - da;
    });

    if (allVideos.length === 0) {
      target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("noRecentUploads")}</div>`;
      return;
    }

    target.innerHTML = "";
    for (const v of allVideos) {
      if (gen !== _renderGen) return; // abort stale render
      const meta = await getVideoMetadata(v.video_id);
      if (gen !== _renderGen) return;
      const card = document.createElement("div");
      card.className = "mts-media-card";
      card.innerHTML = `
        <img src="${meta.thumbnail}" alt="">
        <div class="mts-media-info">
          <div>
            <a class="mts-media-title" href="${getPlaybackLink(v.video_id)}">${escapeHtml(meta.title)}</a>
            <div class="mts-media-meta">${escapeHtml(meta.author)}</div>
            ${renderProgressBadge(v.video_id)}
          </div>
        </div>
      `;
      target.appendChild(card);
    }
  }

  // ---------- UI: Playlists Tab ----------
  async function renderPlaylistsTab(target, gen) {
    if (!appState.backendUrl || !appState.apiToken) {
      target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("notConnectedMsg")}</div>`;
      return;
    }

    target.innerHTML = "";

    // --- Create playlist form ---
    const createRow = document.createElement("div");
    createRow.className = "mts-create-row";
    createRow.innerHTML = `
      <input type="text" class="mts-input" id="mts-new-playlist-name" placeholder="${window.__mts.t('newPlaylistPlaceholder')}">
      <button class="mts-btn mts-btn-primary" data-action="create-playlist">${window.__mts.t("btnNewPlaylist")}</button>
    `;
    target.appendChild(createRow);

    const createBtn = createRow.querySelector('[data-action="create-playlist"]');
    const nameInput = createRow.querySelector("#mts-new-playlist-name");
    const submitCreate = async () => {
      const name = nameInput.value.trim();
      if (!name || name === SYS_LIKED || name === SYS_LATER) return;
      await apiRequest("/playlists", "POST", { name });
      await syncAllState();
    };
    createBtn.addEventListener("click", submitCreate);
    nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitCreate();
    });

    if (appState.playlists.length === 0) {
      const empty = document.createElement("div");
      empty.className = "mts-empty-msg";
      empty.textContent = appState.syncStatus === "err"
        ? window.__mts.t("noPlaylists")
        : window.__mts.t("loading");
      target.appendChild(empty);
      return;
    }

    for (const pl of appState.playlists) {
      if (gen !== _renderGen) return; // abort stale render

      const isSystem = pl.name === SYS_LIKED || pl.name === SYS_LATER;
      const isExpanded = !!appState.expandedPlaylists[pl.id];

      const displayName = pl.name === SYS_LIKED
        ? window.__mts.t("playlistLiked")
        : pl.name === SYS_LATER
          ? window.__mts.t("playlistWatchLater")
          : pl.name;

      const row = document.createElement("div");
      row.className = "mts-playlist-row";
      row.innerHTML = `
        <span class="mts-playlist-name">${isSystem ? "⭐" : "📂"} ${escapeHtml(displayName)} ${isExpanded ? "▾" : "▸"}</span>
        <span class="mts-playlist-row-right">
          <span class="mts-media-meta">${(appState.playlistContentsMap[pl.id] || []).length}</span>
          ${!isSystem ? `<button class="mts-remove-btn" data-action="delete-playlist" title="${window.__mts.t("btnDeletePlaylist")}">🗑</button>` : ""}
        </span>
      `;
      row.querySelector(".mts-playlist-name").addEventListener("click", () => {
        appState.expandedPlaylists[pl.id] = !appState.expandedPlaylists[pl.id];
        renderActiveTab();
      });

      const deleteBtn = row.querySelector('[data-action="delete-playlist"]');
      if (deleteBtn) {
        deleteBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm(window.__mts.t("confirmDeletePlaylist", pl.name))) return;
          await apiRequest(`/playlists/${pl.id}`, "DELETE");
          delete appState.expandedPlaylists[pl.id];
          await syncAllState();
        });
      }

      target.appendChild(row);

      if (isExpanded) {
        const itemsContainer = document.createElement("div");
        itemsContainer.className = "mts-playlist-items";
        // Append the container immediately so the playlist rows stay in order
        // even while we await metadata below.
        target.appendChild(itemsContainer);

        const items = appState.playlistContentsMap[pl.id] || [];

        if (items.length === 0) {
          itemsContainer.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("emptyPlaylist")}</div>`;
        } else {
          for (const item of items) {
            if (gen !== _renderGen) return; // abort stale render
            const meta = await getVideoMetadata(item.video_id);
            if (gen !== _renderGen) return;
            const card = document.createElement("div");
            card.className = "mts-media-card";
            card.innerHTML = `
              <img src="${meta.thumbnail}" alt="">
              <div class="mts-media-info">
                <div>
                  <a class="mts-media-title" href="${getPlaybackLink(item.video_id)}">${escapeHtml(meta.title)}</a>
                  <div class="mts-media-meta">${escapeHtml(meta.author)}</div>
                  ${renderProgressBadge(item.video_id)}
                </div>
                <div style="text-align:right; margin-top:4px;">
                  <button class="mts-remove-btn" data-pl="${pl.id}" data-vid="${item.video_id}">${window.__mts.t("btnDeletePlaylist")}</button>
                </div>
              </div>
            `;
            card.querySelector(".mts-remove-btn").addEventListener("click", async (e) => {
              e.stopPropagation();
              await deletePlaylistItem(pl.id, item.video_id);
            });
            itemsContainer.appendChild(card);
          }
        }
      }
    }
  }

  // ---------- UI: History Tab ----------
  async function renderHistoryTab(target, gen) {
    if (!appState.backendUrl || !appState.apiToken) {
      target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("notConnectedMsg")}</div>`;
      return;
    }
    // syncStatus "busy" means the initial fetch is still in flight
    if (appState.history.length === 0 && appState.syncStatus !== "err") {
      target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("loading")}</div>`;
      return;
    }
    if (appState.history.length === 0) {
      target.innerHTML = `<div class="mts-empty-msg">${window.__mts.t("noHistory")}</div>`;
      return;
    }

    target.innerHTML = "";
    for (const track of appState.history) {
      if (gen !== _renderGen) return; // abort stale render
      const meta = await getVideoMetadata(track.video_id);
      if (gen !== _renderGen) return;
      const card = document.createElement("div");
      card.className = "mts-media-card";
      card.innerHTML = `
        <img src="${meta.thumbnail}" alt="">
        <div class="mts-media-info">
          <div>
            <a class="mts-media-title" href="${getPlaybackLink(track.video_id)}">${escapeHtml(meta.title)}</a>
            <div class="mts-media-meta">${escapeHtml(meta.author)}${track.completed ? " · " + window.__mts.t("watched") : ""}</div>
            ${renderProgressBadge(track.video_id)}
          </div>
        </div>
      `;
      target.appendChild(card);
    }
  }

  // ---------- UI: Current Video / Channel Card ----------

  // Tracks whether the "Add to playlist" section is expanded, per video.
  let plSectionExpanded = {};

  function buildVideoActionsHtml(videoId, meta, idPrefix) {
    const plLiked = appState.playlists.find(p => p.name === SYS_LIKED);
    const plLater = appState.playlists.find(p => p.name === SYS_LATER);
    const isLiked = plLiked && (appState.playlistContentsMap[plLiked.id] || []).some(i => i.video_id === videoId);
    const isLater = plLater && (appState.playlistContentsMap[plLater.id] || []).some(i => i.video_id === videoId);

    const trackingIdentity = meta.handle || meta.author;
    const isSubbed = appState.subscriptions.some(s => s.channel_id.toLowerCase() === trackingIdentity.toLowerCase());

    const customPlaylists = appState.playlists.filter(p => p.name !== SYS_LIKED && p.name !== SYS_LATER);
    const isExpanded = !!plSectionExpanded[`${idPrefix}-${videoId}`];

    let plSectionHtml = "";
    if (customPlaylists.length > 0) {
      let rows = "";
      for (const p of customPlaylists) {
        const isSaved = (appState.playlistContentsMap[p.id] || []).some(i => i.video_id === videoId);
        rows += `
          <label class="mts-pl-checkbox-row">
            <input type="checkbox" data-pl-id="${p.id}" ${isSaved ? "checked" : ""}>
            ${escapeHtml(p.name)}
          </label>
        `;
      }
      plSectionHtml = `
        <div class="mts-btn-row">
          <button class="mts-btn" data-action="pl-section-toggle">${isExpanded ? window.__mts.t("btnAddToPlaylistOpen") : window.__mts.t("btnAddToPlaylist")}</button>
        </div>
        <div class="mts-pl-checkbox-list" ${isExpanded ? "" : 'style="display:none;"'}>
          ${rows}
        </div>
      `;
    }

    return {
      html: `
        <div class="mts-btn-row">
          <button class="mts-btn ${isLiked ? "mts-active" : ""}" data-action="like">${isLiked ? window.__mts.t("btnLiked") : window.__mts.t("btnLike")}</button>
          <button class="mts-btn ${isLater ? "mts-active" : ""}" data-action="later">${isLater ? window.__mts.t("btnInWatchLater") : window.__mts.t("btnWatchLater")}</button>
          <button class="mts-btn mts-danger ${isSubbed ? "mts-active" : ""}" data-action="sub">${isSubbed ? window.__mts.t("btnSubscribed") : window.__mts.t("btnSubscribe")}</button>
        </div>
        ${plSectionHtml}
      `,
      bind(container) {
        container.querySelector('[data-action="like"]').addEventListener("click", () =>
          togglePlaylistItem(videoId, plLiked.id, isLiked));
        container.querySelector('[data-action="later"]').addEventListener("click", () =>
          togglePlaylistItem(videoId, plLater.id, isLater));
        container.querySelector('[data-action="sub"]').addEventListener("click", () =>
          isSubbed ? unsubscribeChannel(trackingIdentity) : subscribeChannel(trackingIdentity));

        const sectionToggle = container.querySelector('[data-action="pl-section-toggle"]');
        if (sectionToggle) {
          sectionToggle.addEventListener("click", () => {
            const key = `${idPrefix}-${videoId}`;
            plSectionExpanded[key] = !plSectionExpanded[key];
            renderCurrentCard();
          });
        }

        container.querySelectorAll(".mts-pl-checkbox-list input[type=checkbox]").forEach(cb => {
          cb.addEventListener("change", () => {
            const plId = parseInt(cb.dataset.plId, 10);
            const wasChecked = !cb.checked; // state before this change
            togglePlaylistItem(videoId, plId, wasChecked);
          });
        });
      }
    };
  }

  async function renderCurrentCard() {
    const card = document.getElementById("mts-current-card");
    const mobileActions = document.getElementById("mts-mobile-actions");
    if (!card && !mobileActions) return;

    if (!appState.backendUrl || !appState.apiToken) {
      if (card) { card.className = "mts-empty"; card.innerHTML = ""; }
      if (mobileActions) { mobileActions.className = "mts-empty"; mobileActions.innerHTML = ""; }
      syncMobileActionsHeight();
      return;
    }

    const videoId = getCurrentVideoId();
    const channelInfo = getCurrentChannelInfo();
    const isChannelPage = !!(channelInfo.handle || channelInfo.rawId);

    if (!videoId && !isChannelPage) {
      if (card) { card.className = "mts-empty"; card.innerHTML = ""; }
      if (mobileActions) { mobileActions.className = "mts-empty"; mobileActions.innerHTML = ""; }
      syncMobileActionsHeight();
      return;
    }

    if (videoId) {
      const meta = await getVideoMetadata(videoId);

      if (card) {
        const actions = buildVideoActionsHtml(videoId, meta, "desktop");
        card.className = "";
        card.innerHTML = actions.html;
        actions.bind(card);
      }

      if (mobileActions) {
        const actions = buildVideoActionsHtml(videoId, meta, "mobile");
        mobileActions.className = "";
        mobileActions.innerHTML = actions.html;
        actions.bind(mobileActions);
      }
    } else if (isChannelPage) {
      const handle = channelInfo.handle; // only ever "@..." or null - never UCxxxx

      if (!handle) {
        // On a /channel/UCxxxx page but couldn't resolve an @handle.
        // Refuse to subscribe rather than ever storing a UCxxxx id.
        const msg = `<div class="mts-empty-msg">${window.__mts.t("cantSubscribeNoHandle")}</div>`;
        if (card) { card.className = ""; card.innerHTML = msg; }
        if (mobileActions) { mobileActions.className = ""; mobileActions.innerHTML = msg; }
        syncMobileActionsHeight();
        return;
      }

      const isSubbed = appState.subscriptions.some(s => s.channel_id.toLowerCase() === handle.toLowerCase());
      const subLabel = isSubbed ? window.__mts.t("btnSubscribed") : window.__mts.t("btnSubscribe");
      const btnHtml = `<button class="mts-btn mts-danger ${isSubbed ? "mts-active" : ""}" data-action="sub">${subLabel}</button>`;

      if (card) {
        card.className = "";
        card.innerHTML = `<div class="mts-btn-row">${btnHtml}</div>`;
        card.querySelector('[data-action="sub"]').addEventListener("click", () =>
          isSubbed ? unsubscribeChannel(handle) : subscribeChannel(handle));
      }

      if (mobileActions) {
        mobileActions.className = "";
        mobileActions.innerHTML = btnHtml;
        mobileActions.querySelector('[data-action="sub"]').addEventListener("click", () =>
          isSubbed ? unsubscribeChannel(handle) : subscribeChannel(handle));
      }
    }

    syncMobileActionsHeight();
  }

  // Keeps --mts-actions-height in sync with the real rendered height of
  // #mts-mobile-actions, so the mobile popup never leaves a dead gap above
  // the nav bar (and grows correctly when the playlist checkbox list opens).
  function syncMobileActionsHeight() {
    const mobileActions = document.getElementById("mts-mobile-actions");
    if (!mobileActions) return;
    requestAnimationFrame(() => {
      const height = mobileActions.classList.contains("mts-empty") ? 0 : mobileActions.offsetHeight;
      document.documentElement.style.setProperty("--mts-actions-height", `${height}px`);
    });
  }

  function escapeHtml(str) {
    if (!str) return "";
    return str.replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
  }

  // Expose
  window.__mts = Object.assign(window.__mts, {
    t: (...args) => window.__mtsi18n ? window.__mtsi18n.t(...args) : args[0],
    initI18n: (...args) => window.__mtsi18n ? window.__mtsi18n.initI18n(...args) : Promise.resolve(),
    setLang: (...args) => window.__mtsi18n ? window.__mtsi18n.setLang(...args) : Promise.resolve(),
    getCurrentLang: () => window.__mtsi18n ? window.__mtsi18n.getCurrentLang() : "en",
    appState,
    getHeaders,
    apiRequest,
    getVideoMetadata,
    getPlaybackLink,
    renderProgressBadge,
    loadSettings,
    syncAllState,
    updateSyncIndicators,
    togglePlaylistItem,
    deletePlaylistItem,
    subscribeChannel,
    unsubscribeChannel,
    updateHistoryState,
    getCurrentVideoId,
    getCurrentChannelInfo,
    isFullscreenActive,
    buildSidebar,
    buildMobileBar,
    setActiveTab,
    renderActiveTab,
    renderCurrentCard,
    openMobilePopup,
    closeMobilePopup,
    getActiveTabId: () => desktopTabId, // kept for backwards compat; mobile has its own state
    SYS_LIKED,
    SYS_LATER,
    HISTORY_INTERVAL_MS
  });
})();
