import * as React from "react"
import { RefreshCwIcon, RotateCcwIcon, Trash2Icon } from "lucide-react"

import { DomainChips } from "@/components/domain-chips"
import { MapHud } from "@/components/map-hud"
import { MapLayersPanel } from "@/components/map-layers"
import { Button } from "@/components/ui/button"
import {
  installParentLocalMapBridge,
  localMapEmbedSrc,
  type HudSnapshot,
  type MapDomain,
  type MapLayers,
} from "@/lib/localmap"

const DEFAULT_LAYERS: MapLayers = {
  env: true,
  occ: false,
  robot: true,
  trail: true,
  follow: true,
  exterior: true,
}

function seedDomains(): MapDomain[] {
  return [
    { id: "assembly-factory", name: "Assembly Factory" },
    { id: "warehouse-bay-a", name: "Warehouse Bay A" },
    { id: "distribution-hub", name: "Distribution Hub" },
    { id: "office", name: "Office" },
  ]
}

export function LocalMapPanel() {
  const iframeRef = React.useRef<HTMLIFrameElement>(null)
  const [domains, setDomains] = React.useState<MapDomain[]>(seedDomains)
  const [activeId, setActiveId] = React.useState<string | null>("warehouse-bay-a")
  const [hud, setHud] = React.useState<HudSnapshot | null>(null)
  const [layers, setLayers] = React.useState<MapLayers>(DEFAULT_LAYERS)
  const [ready, setReady] = React.useState(false)

  const wantLive = React.useMemo(() => {
    const q = new URLSearchParams(window.location.search)
    return q.get("live") === "1"
  }, [])

  const embedSrc = React.useMemo(
    () => localMapEmbedSrc(activeId || "warehouse-bay-a", wantLive),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only initial domain in iframe URL
    [wantLive]
  )

  React.useEffect(() => {
    return installParentLocalMapBridge(() => iframeRef.current)
  }, [])

  const pullHud = React.useCallback(() => {
    const api = iframeRef.current?.contentWindow?.k1LocalMap
    if (!api?.getHudSnapshot) return
    try {
      const snap = api.getHudSnapshot()
      setHud(snap)
      if (snap.domains?.length) setDomains(snap.domains)
      if (snap.active) setActiveId(snap.active)
      if (snap.layers) setLayers(snap.layers)
    } catch {
      /* ignore cross-document race */
    }
  }, [])

  React.useEffect(() => {
    function onMessage(ev: MessageEvent) {
      const data = ev.data
      if (!data || data.source !== "k1-localmap") return
      if (data.type === "k1-ready") {
        setReady(true)
        pullHud()
      }
      if (data.type === "k1-domain") {
        if (Array.isArray(data.domains)) setDomains(data.domains)
        if (data.active) {
          setActiveId(data.active)
          document.title = `k1domain:active:${encodeURIComponent(data.active)}`
          if (typeof window.k1LocalMapOnDomainChange === "function") {
            try {
              window.k1LocalMapOnDomainChange(data.active)
            } catch {
              /* host may override */
            }
          }
        }
        pullHud()
      }
      if (data.type === "k1-layers" && data.layers) {
        setLayers(data.layers as MapLayers)
      }
    }
    window.addEventListener("message", onMessage)
    return () => window.removeEventListener("message", onMessage)
  }, [pullHud])

  React.useEffect(() => {
    if (!ready) return
    const id = window.setInterval(pullHud, 400)
    return () => window.clearInterval(id)
  }, [ready, pullHud])

  function api() {
    return iframeRef.current?.contentWindow?.k1LocalMap
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-card/60 px-3 py-2 backdrop-blur-sm">
        <div className="text-[11px] font-bold tracking-[0.18em] text-muted-foreground uppercase">
          <span className="text-foreground">K1</span> Local Map
        </div>
        <DomainChips
          domains={domains}
          activeId={activeId}
          onSelect={(id) => {
            void api()?.switchDomain(id)
          }}
          onCreate={async (name) => {
            await api()?.createDomain(name)
            pullHud()
          }}
        />
        <div className="flex items-center gap-1.5">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void api()?.loadSample()}
          >
            <RefreshCwIcon data-icon="inline-start" />
            Sample
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => api()?.resetView()}
          >
            <RotateCcwIcon data-icon="inline-start" />
            Reset
          </Button>
          <Button
            type="button"
            size="sm"
            variant="destructive"
            onClick={() => api()?.clear()}
          >
            <Trash2Icon data-icon="inline-start" />
            Clear
          </Button>
        </div>
      </div>

      <div className="relative min-h-[520px] flex-1 bg-black">
        <iframe
          ref={iframeRef}
          title="K1 Local Map Three.js"
          src={embedSrc}
          className="absolute inset-0 size-full border-0"
          allow="fullscreen"
        />
        <div className="pointer-events-none absolute top-4 left-4 z-10">
          <MapHud hud={hud} />
        </div>
        <div className="absolute top-4 right-4 z-10">
          <MapLayersPanel
            layers={layers}
            onToggle={(id, on) => {
              api()?.setLayer(id, on)
              setLayers((prev) => ({ ...prev, [id]: on }))
            }}
          />
        </div>
        <p className="pointer-events-none absolute right-4 bottom-3 z-10 font-mono text-[11px] text-muted-foreground">
          Drag to orbit · Scroll zoom
        </p>
      </div>
    </div>
  )
}
