/* Orca 1.4.216 renderer bridge. Load in Orca's MAIN renderer, never a browser tab.
 * This file is inert until installOrcaLedBridge(options) is called. It neither
 * patches the app bundle nor starts/answers agents. The module names and export
 * symbols are pinned to app build 84daac2de447bf1966ea573fae03ff796ce5779f.
 * Requires options.publish(snapshot), supplied by the trusted host integration.
 */
(() => {
  const KEY = "__orcaLedBridgeV1";
  const VERSION = "1.4.216";
  const BUILD_MODULES = {
    activation: "./assets/worktree-activation-D6bsPnag.js",
    store: "./assets/store-U3p5kNAJ.js",
  };

  globalThis.installOrcaLedBridge = async function installOrcaLedBridge(options) {
    if (!options || typeof options.publish !== "function") {
      throw new Error("A trusted snapshot publisher is required.");
    }
    if (!globalThis.api?.runtime?.getStatus || !globalThis.api?.updater?.getVersion || !document?.baseURI) {
      throw new Error("Run this only in Orca's main desktop renderer.");
    }
    const [status, appVersion] = await Promise.all([
      api.runtime.getStatus(), api.updater.getVersion(),
    ]);
    if (appVersion !== VERSION || !status.runtimeId || !status.authoritativeWindowId) {
      throw new Error("This bridge requires the verified Orca 1.4.216 desktop build.");
    }
    const [activation, storeModule] = await Promise.all([
      import(new URL(BUILD_MODULES.activation, document.baseURI).href),
      import(new URL(BUILD_MODULES.store, document.baseURI).href),
    ]);
    const store = storeModule.t;
    if (typeof activation.d !== "function" || typeof store?.getState !== "function") {
      throw new Error("Orca renderer exports changed; bridge refused to start.");
    }
    if (globalThis[KEY]) await globalThis[KEY].stop();
    const producerId = crypto.randomUUID();
    let stopped = false, sequence = 0, timer = null, active = null;
    let lastError = null;
    const sample = () => {
      // These exact exports are called by Orca's Cmd+1..9 handler. Do not sort.
      const rows = activation.d();
      const state = store.getState();
      if (!Array.isArray(rows) || typeof state.getKnownWorktreeById !== "function") {
        throw new Error("Orca did not return its current numbered workspace order.");
      }
      const slots = rows.map(row => {
        if (typeof row?.id !== "string" || !row.id) throw new Error("Invalid workspace ID.");
        const workspace = state.getKnownWorktreeById(row.id, row.executionHostId);
        const label = workspace?.displayName || workspace?.branch || workspace?.name || row.id;
        return { id: row.id, label: String(label).slice(0, 512),
          execution_host_id: row.executionHostId || "local" };
      });
      return {
        schema: 1, source: "orca-renderer", producer_id: producerId,
        sequence: ++sequence, captured_at: Date.now() / 1000,
        runtime_id: status.runtimeId, app_version: VERSION,
        window_id: status.authoritativeWindowId,
        selected_id: state.activeWorktreeId || null,
        selected_host_id: state.activeWorkspaceExecutionHostId || "local",
        active_view: state.activeView, modal_open: state.activeModal !== "none", slots,
      };
    };
    const tick = async () => {
      if (stopped) return;
      try {
        await options.publish(sample());
        lastError = null;
      } catch (error) {
        // Fail closed: consumers expire the last successful snapshot in 3s.
        lastError = String(error?.message || error);
      } finally {
        if (!stopped) timer = setTimeout(() => { active = tick(); }, 500);
      }
    };
    const bridge = {
      sample,
      status: () => ({ running: !stopped, sequence, lastError, producer_id: producerId }),
      stop: async () => {
        stopped = true;
        clearTimeout(timer);
        if (active) await active;
        if (globalThis[KEY] === bridge) delete globalThis[KEY];
      },
    };
    globalThis[KEY] = bridge;
    // Surface setup/write failures immediately, not an apparently running bridge.
    await options.publish(sample());
    active = tick();
    return bridge;
  };
})();
