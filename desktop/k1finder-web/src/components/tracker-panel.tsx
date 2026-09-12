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
      ? "text-destructive"
      : tone === "accent"
        ? "text-foreground"
        : tone === "warning"
          ? "text-warning"
          : tone === "muted"
            ? "text-muted-foreground"
            : "text-foreground"

  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-center gap-2.5 rounded-xl border border-white/6 bg-white/[0.02] px-3 py-2.5 transition-colors hover:bg-white/[0.04]"
    >
      <Checkbox id={id} defaultChecked={defaultChecked} />
      <Label htmlFor={id} className={`cursor-pointer text-sm font-medium ${color}`}>
        {label}
      </Label>
    </label>
  )
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <p className="font-mono text-[10px] tracking-[0.18em] text-muted-foreground uppercase">
      {children}
    </p>
  )
}

export function TrackerPanel() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 p-6 sm:p-8 lg:grid lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)] lg:gap-6">
      <div className="flex min-h-0 flex-col gap-5">
        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Primary</SectionTitle>
          <div className="mt-4 flex flex-wrap gap-3">
            <Button type="button" variant="outline" className="h-10 min-w-36 rounded-xl px-5">
              Follow: OFF
            </Button>
            <Button type="button" variant="destructive" className="h-10 rounded-xl px-5">
              STOP
            </Button>
            <CheckRow id="drive" label="DRIVE (walk)" tone="danger" />
            <CheckRow id="arm" label="ARM MOTION" tone="danger" />
            <CheckRow id="mute" label="Mute lock sound" tone="muted" />
          </div>
          <FieldGroup className="mt-5 grid gap-4 sm:grid-cols-3">
            <Field className="gap-2">
              <FieldLabel htmlFor="trk-ip">IP</FieldLabel>
              <Input
                id="trk-ip"
                defaultValue="192.168.1.81"
                className="h-10 rounded-xl font-mono"
              />
            </Field>
            <Field className="gap-2">
              <FieldLabel htmlFor="trk-dist">Distance</FieldLabel>
              <Input
                id="trk-dist"
                defaultValue="1.2 m"
                className="h-10 rounded-xl font-mono"
              />
            </Field>
            <Field className="gap-2">
              <FieldLabel htmlFor="trk-spd">Speed</FieldLabel>
              <Input
                id="trk-spd"
                defaultValue="0.18 m/s"
                className="h-10 rounded-xl font-mono"
              />
            </Field>
          </FieldGroup>
        </section>

        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Avoidance</SectionTitle>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {(
              [
                ["gap", "Gap steer", ["off", "audit", "on"]],
                ["head", "Head scan", ["off", "audit", "on", "patient"]],
                ["hit", "Hit box", ["frac", "footprint"], "footprint"],
                ["escape", "Escape", ["off", "spin", "spin+back"]],
              ] as const
            ).map(([id, label, options, selected]) => (
              <Field key={id} className="gap-2">
                <FieldLabel>{label}</FieldLabel>
                <Select defaultValue={selected || options[0]}>
                  <SelectTrigger className="h-10 w-full rounded-xl">
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
          </div>
          <div className="mt-4 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
            <CheckRow id="obs" label="Obstacle brake" tone="accent" defaultChecked />
            <CheckRow id="class" label="Class brake" />
            <CheckRow id="floor" label="Floor reject" tone="accent" />
            <CheckRow id="lmap" label="Local map" tone="accent" defaultChecked />
            <CheckRow id="massist" label="Map assist" tone="accent" />
            <CheckRow id="hprobe" label="Head probe" tone="accent" defaultChecked />
          </div>
        </section>

        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Commands</SectionTitle>
          <div className="mt-4 flex flex-wrap gap-2.5">
            {["WAIT", "RESUME", "PARK", "STATUS", "FOLLOW"].map((c) => (
              <Button
                key={c}
                type="button"
                variant="outline"
                disabled
                className="h-10 rounded-xl px-4"
              >
                {c}
              </Button>
            ))}
            <Button type="button" variant="outline" className="h-10 rounded-xl px-4">
              Open .rrd
            </Button>
          </div>
        </section>
      </div>

      <div className="flex min-h-0 flex-col gap-4">
        <div className="relative flex min-h-72 flex-1 items-center justify-center overflow-hidden rounded-3xl border border-white/8 bg-[#050507] soft-grid">
          <span className="absolute top-4 left-4 rounded-full border border-white/10 bg-black/50 px-3 py-1.5 font-mono text-[10px] tracking-[0.14em] text-muted-foreground uppercase backdrop-blur">
            Preview
          </span>
          <p className="font-mono text-xs text-muted-foreground">annotated camera stream</p>
        </div>
        <div className="grid h-14 place-items-center rounded-2xl border border-white/8 bg-secondary/80 text-lg font-semibold tracking-[0.14em]">
          IDLE
        </div>
        <pre className="min-h-28 overflow-auto rounded-3xl border border-white/8 bg-black/35 p-5 font-mono text-xs leading-relaxed text-muted-foreground">
{`Tracker: set IP, tune Distance + Speed, then toggle Follow.
PREVIEW never moves; tick DRIVE + ARM (confirm) to WALK.`}
        </pre>
      </div>
    </div>
  )
}
