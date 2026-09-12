import * as React from "react"
import { PlusIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { MapDomain } from "@/lib/localmap"
import { cn } from "@/lib/utils"

type Props = {
  domains: MapDomain[]
  activeId: string | null
  onSelect: (id: string) => void
  onCreate: (name: string) => void | Promise<void>
}

export function DomainChips({ domains, activeId, onSelect, onCreate }: Props) {
  const [open, setOpen] = React.useState(false)
  const [name, setName] = React.useState("")
  const [pending, setPending] = React.useState(false)

  async function submit() {
    const trimmed = name.trim()
    if (!trimmed) return
    setPending(true)
    try {
      if (window.k1LocalMapHostCreateDomain) {
        window.k1LocalMapHostCreateDomain({ name: trimmed })
      }
      await onCreate(trimmed)
      setOpen(false)
      setName("")
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto">
      {domains.map((d) => {
        const active = d.id === activeId
        return (
          <Button
            key={d.id}
            type="button"
            size="sm"
            variant={active ? "default" : "outline"}
            aria-pressed={active}
            title={`${d.name || d.id} · ${d.cell_count || 0} cells · ${d.run_count || 0} runs`}
            onClick={() => onSelect(d.id)}
            className={cn(
              "shrink-0 rounded-sm",
              active && "shadow-[0_0_0_1px_color-mix(in_oklab,var(--primary)_35%,transparent)]"
            )}
          >
            {d.name || d.id}
          </Button>
        )
      })}
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="shrink-0 rounded-sm border-dashed border-primary/40 text-primary"
        onClick={() => setOpen(true)}
      >
        <PlusIcon data-icon="inline-start" />
        New domain
      </Button>
      <Badge variant="outline" className="ml-auto shrink-0 font-mono text-[10px] tracking-wide">
        domains · separate maps
      </Badge>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New domain</DialogTitle>
            <DialogDescription>
              Separate map for a new environment. Follow / capture runs merge into
              the active domain over time. New domains start empty.
            </DialogDescription>
          </DialogHeader>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="domain-name">Domain name</FieldLabel>
              <Input
                id="domain-name"
                value={name}
                maxLength={64}
                placeholder="e.g. Warehouse Bay B"
                className="font-mono"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void submit()
                }}
              />
            </Field>
          </FieldGroup>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="button" disabled={pending || !name.trim()} onClick={() => void submit()}>
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
