import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import type { HudSnapshot } from "@/lib/localmap"
import { cn } from "@/lib/utils"

type Props = {
  hud: HudSnapshot | null
}

function liveTone(className: string) {
  if (className.includes("on")) return "bg-success"
  if (className.includes("warn")) return "bg-warning"
  return "bg-destructive"
}

export function MapHud({ hud }: Props) {
  const pose = hud?.pose
  const live = hud?.live

  return (
    <Card className="pointer-events-none w-[min(320px,88vw)] rounded-3xl border-white/10 bg-card/70 shadow-[0_16px_40px_rgb(0_0_0/0.35)] backdrop-blur-xl">
      <CardHeader className="gap-3 px-5 pt-5 pb-3">
        <div className="flex items-center gap-2.5 font-mono text-[10px] tracking-[0.12em] text-muted-foreground uppercase">
          <span
            className={cn("size-2 rounded-full", liveTone(live?.className || "off"))}
            aria-hidden
          />
          <span>
            <span className="text-foreground">{live?.state || "OFFLINE"}</span>
            {" · "}
            {live?.detail || "poll"}
          </span>
        </div>
        <div>
          <CardTitle className="text-[11px] font-medium tracking-[0.14em] text-muted-foreground uppercase">
            Active domain
          </CardTitle>
          <p className="font-heading mt-1.5 text-xl font-semibold tracking-tight text-foreground">
            {hud?.title || "—"}
          </p>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 px-5 pt-0 pb-5">
        <p className="font-mono text-[11px] leading-relaxed text-muted-foreground">
          {hud?.stats || "loading…"}
        </p>
        <Separator className="bg-white/8" />
        <div className="grid grid-cols-4 gap-2 font-mono text-[10px]">
          {(
            [
              ["X", pose?.x],
              ["Y", pose?.y],
              ["YAW", pose?.yaw],
              ["TRAIL", pose?.trail],
            ] as const
          ).map(([k, v]) => (
            <div key={k} className="rounded-2xl bg-secondary/80 px-2.5 py-2.5">
              <span className="mb-1 block tracking-wider text-muted-foreground">{k}</span>
              <span className="text-xs font-semibold text-foreground">{v || "0.00"}</span>
            </div>
          ))}
        </div>
        <div className="grid grid-cols-3 gap-2 font-mono text-[10px]">
          {(
            [
              ["VX", pose?.vx],
              ["VY", pose?.vy],
              ["WZ", pose?.wz],
            ] as const
          ).map(([k, v]) => (
            <div key={k} className="rounded-2xl bg-secondary/80 px-2.5 py-2.5">
              <span className="mb-1 block tracking-wider text-muted-foreground">{k}</span>
              <span className="text-[11px] font-semibold text-foreground">{v || "0.00"}</span>
            </div>
          ))}
        </div>
        <Badge
          variant="secondary"
          className="w-fit rounded-full px-3 py-1 font-mono text-[10px]"
        >
          observe only · drive gates unchanged
        </Badge>
      </CardContent>
    </Card>
  )
}
