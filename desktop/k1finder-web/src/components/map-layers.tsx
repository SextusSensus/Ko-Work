import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { MapLayers } from "@/lib/localmap"

const LAYER_META: { id: keyof MapLayers; label: string }[] = [
  { id: "env", label: "Environment" },
  { id: "occ", label: "Occupancy" },
  { id: "robot", label: "Robot" },
  { id: "trail", label: "Odom trail" },
  { id: "follow", label: "Follow pose" },
  { id: "exterior", label: "Exterior" },
]

type Props = {
  layers: MapLayers
  onToggle: (id: keyof MapLayers, on: boolean) => void
}

export function MapLayersPanel({ layers, onToggle }: Props) {
  const value = LAYER_META.filter((l) => layers[l.id]).map((l) => l.id)

  return (
    <div className="rounded-3xl border border-white/10 bg-card/70 p-2 shadow-[0_16px_40px_rgb(0_0_0/0.35)] backdrop-blur-xl">
      <p className="px-3 pt-2 pb-2 font-mono text-[10px] tracking-[0.16em] text-muted-foreground uppercase">
        Layers
      </p>
      <ToggleGroup
        type="multiple"
        orientation="vertical"
        variant="outline"
        size="sm"
        spacing={1}
        value={value}
        onValueChange={(next) => {
          for (const meta of LAYER_META) {
            const on = next.includes(meta.id)
            if (on !== layers[meta.id]) onToggle(meta.id, on)
          }
        }}
        aria-label="Layer toggles"
        className="w-[168px] items-stretch"
      >
        {LAYER_META.map((meta) => (
          <ToggleGroupItem
            key={meta.id}
            value={meta.id}
            className="h-9 justify-start rounded-xl px-3 text-xs font-medium tracking-[0.04em] data-[state=on]:border-white/20 data-[state=on]:bg-primary data-[state=on]:text-primary-foreground"
            title={
              meta.id === "exterior"
                ? "Show/hide building envelope walls. Default ON."
                : undefined
            }
          >
            <span
              className="mr-2.5 inline-block size-1.5 rounded-full bg-muted-foreground data-[state=on]:bg-primary-foreground"
              data-state={layers[meta.id] ? "on" : "off"}
              aria-hidden
            />
            {meta.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
    </div>
  )
}
