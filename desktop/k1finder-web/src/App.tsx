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
    <div className="mx-auto flex min-h-svh max-w-[1200px] flex-col border-x border-border/80 bg-background/95">
      <div className="h-px bg-gradient-to-r from-transparent via-foreground/40 to-transparent" />
      <header className="animate-in fade-in slide-in-from-top-2 px-5 pt-5 pb-3 duration-500">
        <p className="font-mono text-[10px] tracking-[0.22em] text-muted-foreground uppercase">
          Mission control
        </p>
        <h1 className="font-heading mt-1 text-[28px] leading-none font-semibold tracking-[-0.02em] text-foreground">
          K1 Finder
        </h1>
        <p className="mt-1.5 text-[13px] tracking-wide text-muted-foreground">
          Discover · drive · observe
        </p>
      </header>
      <Separator className="opacity-60" />
      <Tabs
        value={tab}
        onValueChange={setTab}
        className="flex min-h-0 flex-1 flex-col gap-0"
      >
        <TabsList
          variant="line"
          className="h-auto w-full justify-start rounded-none border-b border-border/80 bg-transparent px-2"
        >
          <TabsTrigger
            value="discover"
            className="cursor-pointer rounded-none px-4 py-3.5 text-muted-foreground transition-colors duration-200 after:bg-foreground data-[state=active]:text-foreground"
          >
            Discover
          </TabsTrigger>
          <TabsTrigger
            value="tracker"
            className="cursor-pointer rounded-none px-4 py-3.5 text-muted-foreground transition-colors duration-200 after:bg-foreground data-[state=active]:text-foreground"
          >
            Tracker
          </TabsTrigger>
          <TabsTrigger
            value="map"
            className="cursor-pointer rounded-none px-4 py-3.5 text-muted-foreground transition-colors duration-200 after:bg-foreground data-[state=active]:text-foreground"
          >
            Local Map
          </TabsTrigger>
        </TabsList>
        <TabsContent value="discover" className="mt-0 flex min-h-0 flex-1 flex-col">
          <DiscoverPanel />
        </TabsContent>
        <TabsContent value="tracker" className="mt-0 flex min-h-0 flex-1 flex-col">
          <TrackerPanel />
        </TabsContent>
        <TabsContent
          value="map"
          className="mt-0 flex min-h-0 flex-1 flex-col data-[state=inactive]:hidden"
        >
          <LocalMapPanel />
        </TabsContent>
      </Tabs>
      <footer className="border-t border-border/80 bg-[#0b0b10] px-4 py-2.5 font-mono text-[11px] text-muted-foreground">
        Ready · Space Tech chrome · Local Map embed
      </footer>
      <Toaster theme="dark" />
    </div>
  )
}

export default App
