import * as React from "react"

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
import { click, setValue, useHost, useLiveInput, type Ctl } from "@/lib/host"

type Tone = "danger" | "accent" | "muted" | "warning"

function toneClass(tone?: Tone) {
  if (tone === "danger") return "text-destructive"
  if (tone === "warning") return "text-warning"
  if (tone === "muted") return "text-muted-foreground"
  return "text-foreground"
}

/** A classic K1Finder CheckBox, mirrored: shows the host state, and a click sets it on the host. */
function HostCheck({ id, ctl, label, tone }: { id: string; ctl?: Ctl; label: string; tone?: Tone }) {
  const disabled = !ctl || !ctl.e
  const htmlId = `hc-${id}`
  return (
    <label
      htmlFor={htmlId}
      className={`flex items-center gap-2.5 rounded-xl border border-white/6 bg-white/[0.02] px-3 py-2.5 transition-colors ${
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer hover:bg-white/[0.04]"
      }`}
    >
      <Checkbox
        id={htmlId}
        checked={ctl?.k ?? false}
        disabled={disabled}
        onCheckedChange={(v) => setValue(id, v === true)}
      />
      <Label htmlFor={htmlId} className={`cursor-pointer text-sm font-medium ${toneClass(tone)}`}>
        {label}
      </Label>
    </label>
  )
}

/** A classic K1Finder ComboBox, mirrored. */
function HostSelect({ id, ctl, label }: { id: string; ctl?: Ctl; label: string }) {
  const options = ctl?.o ?? []
  const value = typeof ctl?.v === "string" && ctl.v ? ctl.v : undefined
  return (
    <Field className="gap-2">
      <FieldLabel>{label}</FieldLabel>
      <Select value={value} onValueChange={(v) => setValue(id, v)} disabled={!ctl?.e}>
        <SelectTrigger className="h-10 w-full rounded-xl">
          <SelectValue placeholder="-" />
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
  )
}

/** A classic K1Finder TrackBar, mirrored. */
function HostSlider({
  id,
  ctl,
  label,
  format,
}: {
  id: string
  ctl?: Ctl
  label: string
  format: (n: number) => string
}) {
  const [local, setLocal] = React.useState<number | null>(null)
  const value = local ?? (typeof ctl?.v === "number" ? ctl.v : (ctl?.min ?? 0))
  const htmlId = `hs-${id}`
  return (
    <Field className="gap-2">
      <FieldLabel htmlFor={htmlId}>
        {label} <span className="font-mono text-muted-foreground">{format(value)}</span>
      </FieldLabel>
      <input
        id={htmlId}
        type="range"
        min={ctl?.min ?? 0}
        max={ctl?.max ?? 100}
        step={1}
        value={value}
        disabled={!ctl?.e}
        onChange={(e) => {
          const n = Number(e.target.value)
          setLocal(n)
          setValue(id, n)
        }}
        onPointerUp={() => setLocal(null)}
        onBlur={() => setLocal(null)}
        className="h-10 w-full cursor-pointer accent-primary disabled:cursor-not-allowed disabled:opacity-50"
      />
    </Field>
  )
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <p className="font-mono text-[10px] tracking-[0.18em] text-muted-foreground uppercase">
      {children}
    </p>
  )
}

const COMMANDS = [
  ["btnWait", "WAIT"],
  ["btnResume", "RESUME"],
  ["btnPark", "PARK"],
  ["btnStatus", "STATUS"],
  ["btnFollowCmd", "FOLLOW"],
] as const

export function TrackerPanel() {
  const { state, logs, frame, available } = useHost()
  const c = state?.c ?? {}
  const follow = c.trackToggle
  const following = follow?.k ?? false
  const driving = following && (c.trackDriveChk?.k ?? false)
  const ip = useLiveInput(typeof c.ipTrack?.v === "string" ? c.ipTrack.v : undefined)

  const logRef = React.useRef<HTMLPreElement>(null)
  React.useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [logs.tracker])

  const commitIp = () => {
    ip.setEditing(false)
    setValue("ipTrack", ip.value.trim())
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 p-6 sm:p-8 lg:grid lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)] lg:gap-6">
      <div className="flex min-h-0 flex-col gap-5">
        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Primary</SectionTitle>
          <div className="mt-4 flex flex-wrap gap-3">
            <Button
              type="button"
              variant={following ? (driving ? "destructive" : "default") : "outline"}
              className="h-10 min-w-36 rounded-xl px-5"
              disabled={!available || !follow?.e}
              onClick={() => {
                commitIp()
                setValue("trackToggle", !following)
              }}
            >
              {follow?.x || "Follow: OFF"}
            </Button>
            {/* STOP is never disabled here: the host always runs Stop-Tracker for it. */}
            <Button
              type="button"
              variant="destructive"
              className="h-10 rounded-xl px-5"
              disabled={!available}
              onClick={() => click("trackStopBtn")}
            >
              STOP
            </Button>
            <HostCheck id="trackDriveChk" ctl={c.trackDriveChk} label="DRIVE (walk)" tone="danger" />
            <HostCheck id="trackArmChk" ctl={c.trackArmChk} label="ARM MOTION" tone="danger" />
            <HostCheck id="trackMuteChk" ctl={c.trackMuteChk} label="Mute lock sound" tone="muted" />
          </div>
          <FieldGroup className="mt-5 grid gap-4 sm:grid-cols-3">
            <Field className="gap-2">
              <FieldLabel htmlFor="trk-ip">IP</FieldLabel>
              <Input
                id="trk-ip"
                value={ip.value}
                className="h-10 rounded-xl font-mono"
                disabled={!available || c.ipTrack?.e === false}
                onFocus={() => ip.setEditing(true)}
                onChange={(e) => ip.setLocal(e.target.value)}
                onBlur={commitIp}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitIp()
                }}
              />
            </Field>
            <HostSlider
              id="distTrack"
              ctl={c.distTrack}
              label="Distance"
              format={(n) => `${(n / 10).toFixed(1)} m`}
            />
            <HostSlider
              id="spdTrack"
              ctl={c.spdTrack}
              label="Speed"
              format={(n) => `${(n / 100).toFixed(2)} m/s`}
            />
          </FieldGroup>
        </section>

        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Avoidance</SectionTitle>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <HostSelect id="trackGap" ctl={c.trackGap} label="Gap steer" />
            <HostSelect id="trackScan" ctl={c.trackScan} label="Head scan" />
            <HostSelect id="trackHitBox" ctl={c.trackHitBox} label="Hit box" />
            <HostSelect id="trackEscape" ctl={c.trackEscape} label="Escape" />
          </div>
          <div className="mt-4 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
            <HostCheck id="trackObstacle" ctl={c.trackObstacle} label="Obstacle brake" tone="accent" />
            <HostCheck id="trackClassBrake" ctl={c.trackClassBrake} label="Class brake" />
            <HostCheck id="trackFloor" ctl={c.trackFloor} label="Floor reject" tone="accent" />
            <HostCheck id="trackLocalMap" ctl={c.trackLocalMap} label="Local map" tone="accent" />
            <HostCheck id="trackMapAssist" ctl={c.trackMapAssist} label="Map assist" tone="accent" />
            <HostCheck id="trackHeadProbe" ctl={c.trackHeadProbe} label="Head probe" tone="accent" />
          </div>
        </section>

        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Advanced</SectionTitle>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <HostSelect id="trackApp" ctl={c.trackApp} label="Perception" />
            <HostCheck id="trackCoast" ctl={c.trackCoast} label="Coast occlusions" />
            <HostCheck id="trackReacq" ctl={c.trackReacq} label="Auto re-acq" />
            <HostCheck id="trackFence" ctl={c.trackFence} label="Range fence" />
            <HostCheck id="trackArmReloc" ctl={c.trackArmReloc} label="Arm re-lock" tone="warning" />
            <HostCheck id="trackHbChk" ctl={c.trackHbChk} label="Deadman HB" tone="danger" />
          </div>
        </section>

        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Acquisition</SectionTitle>
          <div className="mt-4 grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
            <HostCheck id="chkGesture" ctl={c.chkGesture} label="Gesture lock" tone="accent" />
            <HostCheck id="chkAB" ctl={c.chkAB} label="A/B (compare)" />
            <HostCheck id="chkCrowd" ctl={c.chkCrowd} label="Crowd 2FA lock" tone="accent" />
            <HostCheck id="chkVoice" ctl={c.chkVoice} label="Voice cmds" tone="accent" />
          </div>
        </section>

        <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
          <SectionTitle>Recording and commands</SectionTitle>
          <div className="mt-4 grid gap-2.5 sm:grid-cols-2">
            <HostCheck id="trackRerun" ctl={c.trackRerun} label="Rerun recording" tone="accent" />
            <HostCheck id="trackCtrlRun" ctl={c.trackCtrlRun} label="Controller run (no follow)" />
          </div>
          <div className="mt-4 flex flex-wrap gap-2.5">
            {COMMANDS.map(([id, label]) => (
              <Button
                key={id}
                type="button"
                variant="outline"
                disabled={!available || !c[id]?.e}
                className="h-10 rounded-xl px-4"
                onClick={() => click(id)}
              >
                {label}
              </Button>
            ))}
            <Button
              type="button"
              variant="outline"
              className="h-10 rounded-xl px-4"
              disabled={!available || c.btnRrd?.e === false}
              onClick={() => click("btnRrd")}
            >
              Open .rrd
            </Button>
          </div>
        </section>
      </div>

      <div className="flex min-h-0 flex-col gap-4">
        <div className="relative flex min-h-72 flex-1 items-center justify-center overflow-hidden rounded-3xl border border-white/8 bg-[#050507] soft-grid">
          <span className="absolute top-4 left-4 z-10 rounded-full border border-white/10 bg-black/50 px-3 py-1.5 font-mono text-[10px] tracking-[0.14em] text-muted-foreground uppercase backdrop-blur">
            Preview
          </span>
          {state?.reid.x && (
            <span
              className="absolute top-4 right-4 z-10 rounded-full border border-white/10 px-3 py-1.5 font-mono text-[10px] tracking-[0.1em] text-white"
              style={{ backgroundColor: state.reid.bg || undefined }}
            >
              {state.reid.x}
            </span>
          )}
          {frame ? (
            <img
              src={frame}
              alt="Annotated camera stream"
              className="max-h-full max-w-full object-contain"
            />
          ) : (
            <p className="font-mono text-xs text-muted-foreground">
              {following ? "waiting for camera frames…" : "annotated camera stream"}
            </p>
          )}
        </div>
        <div
          className="grid h-14 place-items-center rounded-2xl border border-white/8 bg-secondary/80 text-lg font-semibold tracking-[0.14em] text-white"
          style={{ backgroundColor: state?.badge.bg || undefined }}
        >
          {state?.badge.x || "IDLE"}
        </div>
        <pre
          ref={logRef}
          className="max-h-72 min-h-28 overflow-auto rounded-3xl border border-white/8 bg-black/35 p-5 font-mono text-xs leading-relaxed whitespace-pre-wrap text-muted-foreground"
        >
          {logs.tracker ||
            "Tracker: set IP, tune Distance + Speed, then toggle Follow.\nPREVIEW never moves; tick DRIVE + ARM (confirm) to WALK."}
        </pre>
      </div>
    </div>
  )
}
