import * as React from "react"

import { DiscoverPanel } from "@/components/discover-panel"
import { LocalMapPanel } from "@/components/local-map-panel"
import { TrackerPanel } from "@/components/tracker-panel"
import { Separator } from "@/components/ui/separator"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Toaster } from "@/components/ui/sonner"

function initialTab() {
  const q = new URLSearchParams(window.location.search)
  const tab = q.get("tab")
  if (tab === "discover" || tab === "tracker" || tab === "map") return tab
  if (q.get("host") === "webview") return "map"
  return "map"
}

export function App() {
  const [tab, setTab] = React.useState(initialTab)

  return (
    <div className="mx-auto flex min-h-svh max-w-[1200px] flex-col border-x border-border bg-background/95 shadow-[0_0_80px_rgba(50,212,255,0.04)]">
      <div className="h-0.5 bg-primary" />
      <header className="animate-in fade-in slide-in-from-top-2 px-5 pt-4 pb-3 duration-500">
        <h1 className="text-[28px] leading-none font-bold tracking-[-0.02em]">K1 Finder</h1>
        <p className="mt-1 text-[13px] tracking-wide text-muted-foreground">
          mission control · discover · drive · observe
        </p>
      </header>
      <Separator />
      <Tabs
        value={tab}
        onValueChange={setTab}
        className="flex min-h-0 flex-1 flex-col gap-0"
      >
        <TabsList
          variant="line"
          className="h-auto w-full justify-start rounded-none border-b border-border bg-transparent px-2"
        >
          <TabsTrigger value="discover" className="px-4 py-3.5 after:bg-primary">
            Discover
          </TabsTrigger>
          <TabsTrigger value="tracker" className="px-4 py-3.5 after:bg-primary">
            Tracker
          </TabsTrigger>
          <TabsTrigger value="map" className="px-4 py-3.5 after:bg-primary">
            Local Map
          </TabsTrigger>
        </TabsList>
        <TabsContent value="discover" className="mt-0 flex min-h-0 flex-1 flex-col">
          <DiscoverPanel />
        </TabsContent>
        <TabsContent value="tracker" className="mt-0 flex min-h-0 flex-1 flex-col">
          <TrackerPanel />
        </TabsContent>
        <TabsContent value="map" className="mt-0 flex min-h-0 flex-1 flex-col data-[state=inactive]:hidden">
          <LocalMapPanel />
        </TabsContent>
      </Tabs>
      <footer className="border-t border-border bg-[#0a0a0a] px-4 py-2 font-mono text-xs text-muted-foreground">
        Ready · Tesla × SpaceX × Apple · shadcn/ui chrome · Three.js Local Map embed
      </footer>
      <Toaster theme="dark" />
    </div>
  )
}

export default App
