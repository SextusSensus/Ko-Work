import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"

function CheckRow({
  id,
  label,
  defaultChecked,
  tone,
}: {
  id: string
  label: string
  defaultChecked?: boolean
  tone?: "danger" | "accent" | "muted" | "warning"
}) {
  const color =
    tone === "danger"
      ? "text-destructive font-bold"
      : tone === "accent"
        ? "text-primary font-bold"
        : tone === "warning"
          ? "text-warning font-semibold"
          : tone === "muted"
            ? "text-muted-foreground"
            : "text-foreground"

  return (
    <div className="flex items-center gap-2">
      <Checkbox id={id} defaultChecked={defaultChecked} />
      <Label htmlFor={id} className={color}>
        {label}
      </Label>
    </div>
  )
}

export function TrackerPanel() {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-3 border-b border-border bg-card px-4 py-3">
        <p className="text-[11px] font-bold tracking-[0.12em] text-muted-foreground uppercase">
          Primary
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" variant="outline" className="min-w-36">
            Follow: OFF
          </Button>
          <CheckRow id="drive" label="DRIVE (walk)" tone="danger" />
          <CheckRow id="arm" label="ARM MOTION" tone="danger" />
          <Button type="button" variant="destructive">
            STOP
          </Button>
          <CheckRow id="mute" label="mute lock sound" tone="muted" />
          <FieldGroup className="flex-row flex-wrap items-end gap-3">
            <Field className="gap-1">
              <FieldLabel htmlFor="trk-ip">IP</FieldLabel>
              <Input id="trk-ip" defaultValue="192.168.1.81" className="w-36 font-mono" />
            </Field>
            <Field className="gap-1">
              <FieldLabel htmlFor="trk-dist">Distance</FieldLabel>
              <Input id="trk-dist" defaultValue="1.2 m" className="w-20 font-mono" />
            </Field>
            <Field className="gap-1">
              <FieldLabel htmlFor="trk-spd">Speed</FieldLabel>
              <Input id="trk-spd" defaultValue="0.18 m/s" className="w-24 font-mono" />
            </Field>
          </FieldGroup>
        </div>

        <Separator />
        <p className="text-[11px] font-bold tracking-[0.12em] text-muted-foreground uppercase">
          Avoidance
        </p>
        <div className="flex flex-wrap items-end gap-3">
          {(
            [
              ["gap", "Gap steer", ["off", "audit", "on"]],
              ["head", "Head scan", ["off", "audit", "on", "patient"]],
              ["hit", "Hit box", ["frac", "footprint"], "footprint"],
              ["escape", "Escape", ["off", "spin", "spin+back"]],
            ] as const
          ).map(([id, label, options, selected]) => (
            <Field key={id} className="gap-1">
              <FieldLabel>{label}</FieldLabel>
              <Select defaultValue={selected || options[0]}>
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {options.map((o) => (
                      <SelectItem key={o} value={o}>
                        {o}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          ))}
          <CheckRow id="obs" label="Obstacle brake" tone="accent" defaultChecked />
          <CheckRow id="class" label="Class brake" />
          <CheckRow id="floor" label="Floor reject" tone="accent" />
          <CheckRow id="lmap" label="Local map" tone="accent" defaultChecked />
          <CheckRow id="massist" label="Map assist" tone="accent" />
          <CheckRow id="hprobe" label="Head probe" tone="accent" defaultChecked />
        </div>

        <Separator />
        <p className="text-[11px] font-bold tracking-[0.12em] text-muted-foreground uppercase">
          Cmd
        </p>
        <div className="flex flex-wrap gap-2">
          {["WAIT", "RESUME", "PARK", "STATUS", "FOLLOW"].map((c) => (
            <Button key={c} type="button" variant="outline" disabled>
              {c}
            </Button>
          ))}
          <Button type="button" variant="outline" className="text-primary">
            Open .rrd
          </Button>
        </div>
      </div>

      <div className="relative mx-4 mt-3 flex min-h-44 flex-1 items-center justify-center border border-border bg-[#050505] font-mono text-xs text-muted-foreground">
        <BadgeLike />
        annotated camera stream
      </div>
      <div className="mx-4 mt-2 grid h-12 place-items-center bg-secondary text-lg font-bold tracking-[0.1em]">
        IDLE
      </div>
      <pre className="mx-4 mt-2 mb-4 h-24 overflow-auto border border-border bg-secondary/40 p-3 font-mono text-xs leading-relaxed text-primary">
{`Tracker: set IP, tune Distance + Speed, then toggle Follow. PREVIEW never moves; tick DRIVE + ARM (confirm) to WALK.`}
      </pre>
    </div>
  )
}

function BadgeLike() {
  return (
    <span className="absolute top-3 left-3 border border-border bg-secondary px-3 py-1.5 text-xs font-bold tracking-[0.08em]">
      PREVIEW
    </span>
  )
}
