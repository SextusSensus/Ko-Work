import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

const robots = [
  {
    id: "unitree",
    name: "Unitree",
    src: "./robots/unitree.png",
  },
  {
    id: "booster",
    name: "Booster",
    src: "./robots/booster.png",
  },
  {
    id: "galbot",
    name: "Galbot",
    src: "./robots/galbot.png",
  },
] as const

export function DiscoverPanel() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 p-6 sm:p-8">
      <section className="rounded-3xl border border-white/8 bg-white/[0.03] p-5 sm:p-6">
        <div className="flex flex-col gap-6 xl:flex-row xl:items-center xl:justify-between xl:gap-8">
          <div className="shrink-0 space-y-1.5">
            <p className="font-mono text-[10px] tracking-[0.18em] text-muted-foreground uppercase">
              Network discovery
            </p>
            <h2 className="font-heading text-xl font-semibold tracking-tight">
              Find Clanker Over LAN
            </h2>
            <p className="text-sm text-muted-foreground">
              Subnets · 192.168.1.0/24 · 10.0.0.0/24
            </p>
          </div>

          <div
            className="flex min-w-0 flex-1 items-end justify-center gap-3 sm:gap-5 md:gap-8"
            aria-label="Supported humanoid platforms"
          >
            {robots.map((robot) => (
              <figure
                key={robot.id}
                className="group flex w-[28%] max-w-[140px] flex-col items-center gap-2 sm:max-w-[160px] md:max-w-[180px]"
              >
                <div className="relative flex h-28 w-full items-end justify-center sm:h-36 md:h-40">
                  <img
                    src={robot.src}
                    alt={robot.name}
                    className="max-h-full max-w-full object-contain drop-shadow-[0_18px_40px_rgba(0,0,0,0.55)] transition duration-500 ease-out group-hover:-translate-y-1 group-hover:scale-[1.03]"
                    draggable={false}
                  />
                </div>
              </figure>
            ))}
          </div>

          <div className="flex shrink-0 flex-wrap items-end gap-3 xl:flex-col xl:items-stretch 2xl:flex-row 2xl:items-end">
            <div className="flex flex-wrap items-end gap-3">
              <Button type="button" size="lg" className="h-10 rounded-xl px-5">
                Scan for K1
              </Button>
              <Button
                type="button"
                size="lg"
                variant="outline"
                className="h-10 rounded-xl px-5"
                disabled
              >
                Stop
              </Button>
            </div>
            <FieldGroup className="flex-row flex-wrap items-end gap-3">
              <Field className="gap-2">
                <FieldLabel htmlFor="k1-ip">Or enter K1 IP</FieldLabel>
                <Input
                  id="k1-ip"
                  defaultValue="192.168.1.81"
                  className="h-10 w-44 rounded-xl font-mono"
                />
              </Field>
              <Button
                type="button"
                size="lg"
                variant="outline"
                className="h-10 rounded-xl px-5"
              >
                Verify
              </Button>
            </FieldGroup>
          </div>
        </div>
      </section>

      <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-3xl border border-white/8 bg-black/20">
        <div className="flex items-center justify-between border-b border-white/6 px-5 py-4 sm:px-6">
          <div>
            <p className="font-mono text-[10px] tracking-[0.16em] text-muted-foreground uppercase">
              Candidates
            </p>
            <p className="mt-1 text-sm text-muted-foreground">Ranked by confidence</p>
          </div>
          <Badge variant="secondary" className="rounded-full px-3 py-1 font-mono text-[10px]">
            2 hosts
          </Badge>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="px-2 py-2 sm:px-4">
            <Table>
              <TableHeader>
                <TableRow className="border-white/6 hover:bg-transparent">
                  <TableHead className="h-11 px-4">Confidence</TableHead>
                  <TableHead className="h-11 px-4">IP Address</TableHead>
                  <TableHead className="h-11 px-4">Hostname</TableHead>
                  <TableHead className="h-11 px-4">SSH Banner</TableHead>
                  <TableHead className="h-11 px-4">Why</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="font-mono text-xs">
                <TableRow className="border-white/6">
                  <TableCell className="px-4 py-4">
                    <Badge className="rounded-full">High</Badge>
                  </TableCell>
                  <TableCell className="px-4 py-4">192.168.1.81</TableCell>
                  <TableCell className="px-4 py-4">booster-k1</TableCell>
                  <TableCell className="px-4 py-4">OpenSSH_8.9</TableCell>
                  <TableCell className="px-4 py-4 text-muted-foreground">
                    banner + hostname match
                  </TableCell>
                </TableRow>
                <TableRow className="border-white/6">
                  <TableCell className="px-4 py-4">
                    <Badge variant="secondary" className="rounded-full text-warning">
                      Medium
                    </Badge>
                  </TableCell>
                  <TableCell className="px-4 py-4">192.168.1.44</TableCell>
                  <TableCell className="px-4 py-4">orin-dev</TableCell>
                  <TableCell className="px-4 py-4">OpenSSH_8.2</TableCell>
                  <TableCell className="px-4 py-4 text-muted-foreground">
                    open 22 only
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </div>
        </ScrollArea>
        <div className="flex flex-wrap gap-3 border-t border-white/6 px-5 py-4 sm:px-6">
          <Button type="button" variant="secondary" className="h-10 rounded-xl px-5">
            Verify Selected
          </Button>
          <Button type="button" variant="outline" className="h-10 rounded-xl px-5">
            Use this IP everywhere
          </Button>
        </div>
      </section>

      <pre className="h-40 overflow-auto rounded-3xl border border-white/8 bg-black/35 p-5 font-mono text-xs leading-relaxed text-muted-foreground">
{`--- Starting scan ---
[+] 192.168.1.81  High  booster-k1
ready.`}
      </pre>
    </div>
  )
}
