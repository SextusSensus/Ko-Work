import * as React from "react"

import { DiscoverPanel } from "@/components/discover-panel"
import { LocalMapPanel } from "@/components/local-map-panel"
import { TrackerPanel } from "@/components/tracker-panel"
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
    <div className="relative mx-auto flex min-h-svh w-full max-w-[1280px] flex-col px-4 py-5 sm:px-6 sm:py-7 lg:px-8">
      <div className="glass-panel flex min-h-0 flex-1 flex-col overflow-hidden rounded-[1.75rem]">
        <Tabs
          value={tab}
          onValueChange={setTab}
          className="flex min-h-0 flex-1 flex-col gap-0"
        >
          <header className="flex flex-col gap-6 px-6 pt-7 pb-5 sm:px-8 sm:pt-8">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-3">
                <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-[10px] tracking-[0.22em] text-muted-foreground uppercase">
                  <span className="size-1.5 rounded-full bg-success" aria-hidden />
                  Mission control
                </div>
                <div>
                  <h1 className="font-heading text-3xl leading-none font-semibold tracking-[-0.03em] text-foreground sm:text-4xl">
                    Sky Connect
                  </h1>
                  <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted-foreground sm:text-[15px]">
                    Discover robots, drive with intent, observe the local map —
                    quiet chrome for serious work.
                  </p>
                </div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/25 px-4 py-3 font-mono text-[11px] text-muted-foreground">
                <div className="text-[10px] tracking-[0.16em] uppercase opacity-70">
                  Status
                </div>
                <div className="mt-1 text-sm text-foreground">Systems nominal</div>
              </div>
            </div>

            <div className="flex items-center justify-center py-2 sm:py-3">
              <img
                src="./robots/hero-galbot.png"
                alt="Galbot"
                className="h-44 w-auto max-w-[min(100%,280px)] object-contain drop-shadow-[0_24px_48px_rgba(0,0,0,0.55)] sm:h-52 md:h-60"
                draggable={false}
              />
            </div>

            <TabsList className="h-auto w-full justify-start gap-1 rounded-2xl border border-white/8 bg-black/30 p-1.5">
              <TabsTrigger
                value="discover"
                className="cursor-pointer rounded-xl px-4 py-2.5 text-sm text-muted-foreground transition-all duration-200 data-[state=active]:bg-primary data-[state=active]:text-primary-foreground data-[state=active]:shadow-sm"
              >
                Discover
              </TabsTrigger>
              <TabsTrigger
                value="tracker"
                className="cursor-pointer rounded-xl px-4 py-2.5 text-sm text-muted-foreground transition-all duration-200 data-[state=active]:bg-primary data-[state=active]:text-primary-foreground data-[state=active]:shadow-sm"
              >
                Tracker
              </TabsTrigger>
              <TabsTrigger
                value="map"
                className="cursor-pointer rounded-xl px-4 py-2.5 text-sm text-muted-foreground transition-all duration-200 data-[state=active]:bg-primary data-[state=active]:text-primary-foreground data-[state=active]:shadow-sm"
              >
                Local Map
              </TabsTrigger>
            </TabsList>
          </header>

          <TabsContent
            value="discover"
            className="mt-0 flex min-h-0 flex-1 flex-col border-t border-white/6"
          >
            <DiscoverPanel />
          </TabsContent>
          <TabsContent
            value="tracker"
            className="mt-0 flex min-h-0 flex-1 flex-col border-t border-white/6"
          >
            <TrackerPanel />
          </TabsContent>
          <TabsContent
            value="map"
            className="mt-0 flex min-h-0 flex-1 flex-col border-t border-white/6 data-[state=inactive]:hidden"
          >
            <LocalMapPanel />
          </TabsContent>
        </Tabs>

        <footer className="flex items-center border-t border-white/6 px-6 py-3.5 font-mono text-[11px] text-muted-foreground sm:px-8">
          <span>Ready</span>
        </footer>
      </div>
      <Toaster theme="dark" />
    </div>
  )
}

export default App
