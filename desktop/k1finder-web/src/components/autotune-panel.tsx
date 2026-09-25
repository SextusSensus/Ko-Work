import * as React from "react"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  COLUMNS,
  MOCK_SNAPSHOT,
  cardsIn,
  type AutotuneBadge,
  type AutotuneCard,
  type AutotuneSnapshot,
  type AutotuneSource,
  type AutotuneTone,
} from "@/lib/autotune"
import { useHost } from "@/lib/host"
import { cn } from "@/lib/utils"

function toneClass(tone?: AutotuneTone) {
  if (tone === "success") return "text-success"
  if (tone === "danger") return "text-destructive"
  if (tone === "muted") return "text-muted-foreground"
  if (tone === "accent") return "text-accent"
  return "text-foreground"
}

function StatusBadge({ badge }: { badge?: AutotuneBadge }) {
  if (!badge) return null
  if (badge === "FAIL") {
    return (
      <Badge
        variant="destructive"
        className="rounded-full text-[10px] tracking-[0.12em]"
      >
        {badge}
      </Badge>
    )
  }
  if (badge === "PASS") {
    return (
      <Badge
        variant="outline"
        className="rounded-full text-[10px] tracking-[0.12em] text-success"
      >
        {badge}
      </Badge>
    )
  }
  return (
    <Badge
      variant="secondary"
      className="rounded-full text-[10px] tracking-[0.12em]"
    >
      {badge}
    </Badge>
  )
}

function RunCard({ card }: { card: AutotuneCard }) {
  return (
    <Card size="sm" className="rounded-2xl bg-black/25 py-3 ring-white/8">
      <CardHeader className="gap-1.5 px-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="font-mono text-[11px] font-medium tracking-tight text-muted-foreground">
            {card.id}
          </CardTitle>
          <StatusBadge badge={card.badge} />
        </div>
        <p className={cn("text-sm font-medium", toneClass(card.tone))}>
          {card.title}
        </p>
        <CardDescription className="text-[13px] leading-relaxed">
          {card.body}
        </CardDescription>
        {card.meta ? (
          <p className="font-mono text-[10px] tracking-wide text-muted-foreground uppercase">
            {card.meta}
          </p>
        ) : null}
      </CardHeader>
    </Card>
  )
}

function formatClock(now: Date) {
  return now.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  })
}

export function AutotunePanel() {
  const { autotune, available } = useHost()
  const [source, setSource] = React.useState<AutotuneSource>("mock")
  const [clock, setClock] = React.useState(() => formatClock(new Date()))

  React.useEffect(() => {
    const id = window.setInterval(() => setClock(formatClock(new Date())), 1000)
    return () => window.clearInterval(id)
  }, [])

  const snapshot: AutotuneSnapshot =
    source === "mock"
      ? MOCK_SNAPSHOT
      : (autotune ?? { cards: [], activity: [] })
  const progress = snapshot.progress
  const pct =
    progress && progress.max > 0
      ? Math.min(100, Math.round((100 * progress.v) / progress.max))
      : 0
  const feedEmpty = source === "feed" && snapshot.cards.length === 0

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 p-6 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-3">
          <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-[10px] tracking-[0.22em] text-muted-foreground uppercase">
            <span className="size-1.5 rounded-full bg-accent" aria-hidden />
            Improve backend
          </div>
          <div>
            <h1 className="font-heading text-3xl leading-none font-semibold tracking-[-0.03em] text-foreground sm:text-4xl">
              Autotune Viz
            </h1>
            <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted-foreground sm:text-[15px]">
              Live view of what is in flight to the robot, what the GPU is
              labelling, and what is waiting in the improve queue.
            </p>
          </div>
        </div>
        <div className="rounded-2xl border border-white/8 bg-black/25 px-4 py-3">
          <div className="font-mono text-[10px] tracking-[0.16em] text-muted-foreground uppercase opacity-70">
            Status
          </div>
          <ToggleGroup
            type="single"
            value={source}
            onValueChange={(value) => {
              if (value === "mock" || value === "feed") setSource(value)
            }}
            variant="outline"
            size="sm"
            spacing={0}
            className="mt-2 rounded-xl border border-white/8 bg-black/40 p-0.5"
            aria-label="Autotune data source"
          >
            <ToggleGroupItem
              value="mock"
              className="cursor-pointer rounded-lg px-3"
            >
              Mock
            </ToggleGroupItem>
            <ToggleGroupItem
              value="feed"
              className="cursor-pointer rounded-lg px-3"
            >
              feed
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
      </div>

      {progress ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <p
              className="font-mono text-[11px] text-muted-foreground"
              role="status"
            >
              {progress.label}
            </p>
            {progress.hint ? (
              <p className="font-mono text-[11px] text-muted-foreground">
                {progress.hint}
              </p>
            ) : null}
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={progress.max}
            aria-valuenow={progress.v}
            aria-label={progress.label}
            className="h-0.5 overflow-hidden rounded-full bg-white/10"
          >
            <div
              className="h-full bg-accent transition-[width] duration-300"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      ) : null}

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-3">
        {COLUMNS.map((column) => {
          const cards = cardsIn(snapshot, column.id)
          return (
            <Card
              key={column.id}
              className="min-h-72 bg-black/20 py-4 ring-white/8"
            >
              <CardHeader className="gap-1 px-4">
                <CardTitle className="font-mono text-[10px] font-medium tracking-[0.18em] text-muted-foreground uppercase">
                  {column.label}
                </CardTitle>
                <CardDescription className="text-[13px]">
                  {column.hint}
                </CardDescription>
              </CardHeader>
              <CardContent className="min-h-0 flex-1 px-4">
                <ScrollArea className="h-full max-h-[min(52vh,28rem)]">
                  <div className="flex flex-col gap-3 pr-2">
                    {cards.map((card) => (
                      <RunCard
                        key={`${card.column}-${card.id}-${card.title}`}
                        card={card}
                      />
                    ))}
                    {column.id === "queue" && feedEmpty ? (
                      <Empty className="border border-dashed border-white/8 py-8">
                        <EmptyHeader>
                          <EmptyTitle>No live feed yet</EmptyTitle>
                          <EmptyDescription>
                            {available
                              ? "Auto-Tune-Loop has not published a snapshot. Mock shows the designed board."
                              : "Open this tab in Sky Connect (WebView2) while the auto-improve loop is running, or stay on Mock."}
                          </EmptyDescription>
                        </EmptyHeader>
                      </Empty>
                    ) : null}
                  </div>
                </ScrollArea>
              </CardContent>
            </Card>
          )
        })}
      </div>

      <Card className="bg-black/25 py-4 ring-white/8">
        <CardHeader className="flex flex-row items-center justify-between gap-3 px-4">
          <CardTitle className="font-mono text-[10px] font-medium tracking-[0.18em] text-muted-foreground uppercase">
            Activity
          </CardTitle>
          <p className="font-mono text-[11px] text-muted-foreground">{clock}</p>
        </CardHeader>
        <CardContent className="px-4">
          <pre
            role="log"
            aria-label="Autotune activity"
            className="max-h-36 overflow-auto rounded-2xl border border-white/6 bg-black/40 p-4 font-mono text-[11px] leading-relaxed text-muted-foreground"
          >
            {snapshot.activity.length
              ? snapshot.activity.join("\n")
              : source === "feed"
                ? "waiting for Auto-Tune-Loop…"
                : ""}
          </pre>
        </CardContent>
      </Card>
    </div>
  )
}
