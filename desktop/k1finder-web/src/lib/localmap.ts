export type MapDomain = {
  id: string
  name?: string
  cell_count?: number
  run_count?: number
  updated?: string
}

export type MapLayers = {
  env: boolean
  occ: boolean
  robot: boolean
  trail: boolean
  follow: boolean
  exterior: boolean
}

export type HudSnapshot = {
  active: string | null
  title: string
  stats: string
  domains: MapDomain[]
  layers: MapLayers
  pose: {
    x: string
    y: string
    yaw: string
    trail: string
    vx: string
    vy: string
    wz: string
  }
  live: {
    state: string
    detail: string
    className: string
  }
}

export type K1LocalMapApi = {
  switchDomain: (id: string) => Promise<unknown>
  createDomain: (name: string) => Promise<unknown>
  openNewDomain: () => void
  listDomains: () => MapDomain[]
  getActiveDomain: () => string | null
  getLayers: () => MapLayers
  setLayer: (id: keyof MapLayers, on: boolean) => boolean
  setExteriorVisible: (on: boolean) => boolean
  setFollowPose: (on: boolean) => boolean
  setShowRobot: (on: boolean) => boolean
  resetView: () => void
  clear: () => void
  loadSample: () => Promise<unknown>
  loadFeed: (path?: string) => Promise<unknown>
  refreshDomains: () => Promise<unknown>
  getHudSnapshot: () => HudSnapshot
  connectTelemetry: (url?: string) => void
  disconnectTelemetry: () => void
}

declare global {
  interface Window {
    k1LocalMap?: K1LocalMapApi
    k1LocalMapHostCreateDomain?: (payload: { name: string }) => void
    k1LocalMapOnDomainChange?: (id: string) => void
  }
}

const API_METHODS = [
  "switchDomain",
  "createDomain",
  "openNewDomain",
  "listDomains",
  "getActiveDomain",
  "getLayers",
  "setLayer",
  "setExteriorVisible",
  "setFollowPose",
  "setShowRobot",
  "resetView",
  "clear",
  "loadSample",
  "loadFeed",
  "refreshDomains",
  "getHudSnapshot",
  "connectTelemetry",
  "disconnectTelemetry",
  "setMap",
  "getControls",
  "getCurrentMap",
  "mergeIntoActive",
  "getLastOdom",
  "getTelemetryState",
  "setRegistry",
  "setPose",
] as const

export function localMapEmbedSrc(domain?: string | null, live = false) {
  const params = new URLSearchParams({ embed: "1" })
  if (domain) params.set("domain", domain)
  if (live) params.set("live", "1")
  const qs = params.toString()
  // Vite dev + preview middleware mounts /localmap-viewer from desktop/
  if (import.meta.env.DEV) {
    return `/localmap-viewer/index.html?${qs}`
  }
  // WinForms WebView2 often loads file://…/k1finder-web/dist/index.html
  if (typeof location !== "undefined" && location.protocol === "file:") {
    return new URL(`../../localmap-viewer/index.html?${qs}`, location.href).href
  }
  // Static/telemetry servers rooted at desktop/, or vite preview middleware
  return `/localmap-viewer/index.html?${qs}`
}

/** Forward WinForms ExecuteScriptAsync calls into the Three.js iframe. */
export function installParentLocalMapBridge(
  getIframe: () => HTMLIFrameElement | null
) {
  const bridge: Record<string, unknown> = {}
  for (const method of API_METHODS) {
    bridge[method] = (...args: unknown[]) => {
      const api = getIframe()?.contentWindow?.k1LocalMap as
        | Record<string, (...a: unknown[]) => unknown>
        | undefined
      const fn = api?.[method]
      if (typeof fn !== "function") return undefined
      return fn.apply(api, args)
    }
  }
  Object.defineProperty(bridge, "_scene", {
    get() {
      return getIframe()?.contentWindow?.k1LocalMap
        ? (getIframe()!.contentWindow as Window & { k1LocalMap: { _scene?: unknown } })
            .k1LocalMap._scene
        : undefined
    },
  })
  window.k1LocalMap = bridge as unknown as K1LocalMapApi
  return () => {
    if (window.k1LocalMap === bridge) delete window.k1LocalMap
  }
}
