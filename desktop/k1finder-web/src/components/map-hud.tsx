import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import type { HudSnapshot } from "@/lib/localmap"
import { cn } from "@/lib/utils"

type Props = {
  hud: HudSnapshot | null
}

function liveTone(className: string) {
  if (className.includes("on")) return "bg-success shadow-[0_0_8px_color-mix(in_oklab,var(--success)_55%,transparent)]"
  if (className.includes("warn")) return "bg-warning"
  return "bg-destructive"
}

export function MapHud({ hud }: Props) {
  const pose = hud?.pose
  const live = hud?.live

  return (
    <Card className="pointer-events-none w-[min(300px,86vw)] border-border/80 bg-card/80 shadow-none backdrop-blur-md">
      <CardHeader className="gap-2 pb-2">
        <div className="flex items-center gap-2 font-mono text-[10px] tracking-[0.1em] text-muted-foreground uppercase">
          <span
            className={cn("size-1.5 rounded-full", liveTone(live?.className || "off"))}
            aria-hidden
          />
          <span>
            <span className="text-foreground">{live?.state || "OFFLINE"}</span>
            {" · "}
            {live?.detail || "poll"}
          </span>
        </div>
        <CardTitle className="text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase">
          Active domain <span className="text-primary">·</span>
        </CardTitle>
        <p className="text-lg font-semibold tracking-tight text-foreground">
          {hud?.title || "—"}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 pt-0">
        <p className="font-mono text-[11px] leading-relaxed text-muted-foreground">
          {hud?.stats || "loading…"}
        </p>
        <Separator />
        <div className="grid grid-cols-4 gap-1.5 font-mono text-[10px]">
          {(
            [
              ["X", pose?.x],
              ["Y", pose?.y],
              ["YAW", pose?.yaw],
              ["TRAIL", pose?.trail],
            ] as const
          ).map(([k, v]) => (
            <div key={k} className="rounded-md bg-secondary/60 px-1.5 py-1.5">
              <span className="mb-0.5 block tracking-wider text-muted-foreground">{k}</span>
              <span className="text-xs font-semibold text-primary">{v || "0.00"}</span>
            </div>
          ))}
        </div>
        <div className="grid grid-cols-3 gap-1.5 font-mono text-[10px]">
          {(
            [
              ["VX", pose?.vx],
              ["VY", pose?.vy],
              ["WZ", pose?.wz],
            ] as const
          ).map(([k, v]) => (
            <div key={k} className="rounded-md bg-secondary/60 px-1.5 py-1.5">
              <span className="mb-0.5 block tracking-wider text-muted-foreground">{k}</span>
              <span className="text-[11px] font-semibold text-foreground">{v || "0.00"}</span>
            </div>
          ))}
        </div>
        <Badge variant="secondary" className="w-fit font-mono text-[10px]">
          observe only · drive gates unchanged
        </Badge>
      </CardContent>
    </Card>
  )
}
