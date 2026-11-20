import { Slot } from '@radix-ui/react-slot'
import * as RadixTabs from '@radix-ui/react-tabs'
import { cva, type VariantProps } from 'class-variance-authority'
import { clsx } from 'clsx'
import type { ComponentProps } from 'react'
import { twMerge } from 'tailwind-merge'

function cn(...classes: Parameters<typeof clsx>) {
  return twMerge(clsx(...classes))
}

const buttonVariants = cva(
  'inline-flex shrink-0 items-center justify-center gap-2 rounded-lg text-sm font-semibold outline-none transition-colors focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-45',
  {
    variants: {
      variant: {
        default: 'bg-primary text-primary-foreground hover:bg-primary/85',
        outline: 'border border-border bg-card text-foreground hover:bg-muted',
        ghost: 'text-muted-foreground hover:bg-muted hover:text-foreground',
      },
      size: { default: 'min-h-11 px-3.5', icon: 'size-11' },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
)

export function Button({
  className, variant, size, asChild, ...props
}: ComponentProps<'button'> & VariantProps<typeof buttonVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : 'button'
  return <Comp data-slot="button" className={cn(buttonVariants({ variant, size }), className)} {...props} />
}

const badgeVariants = cva('inline-flex shrink-0 items-center rounded px-1.5 py-0.5 font-mono text-[0.68rem] font-medium uppercase leading-5 tracking-wider', {
  variants: {
    variant: {
      default: 'bg-muted text-muted-foreground',
      accent: 'bg-primary/12 text-primary',
      warning: 'bg-warning/12 text-warning',
    },
  },
  defaultVariants: { variant: 'default' },
})

export function Badge({ className, variant, ...props }: ComponentProps<'span'> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />
}

export const Tabs = RadixTabs.Root

export function TabsList({ className, ...props }: ComponentProps<typeof RadixTabs.List>) {
  return <RadixTabs.List data-slot="tabs-list" className={cn('grid grid-cols-3 gap-1 rounded-lg border border-border bg-card p-1', className)} {...props} />
}

export function TabsTrigger({ className, ...props }: ComponentProps<typeof RadixTabs.Trigger>) {
  return (
    <RadixTabs.Trigger data-slot="tabs-trigger" className={cn(
      'min-h-10 rounded-md px-2 font-mono text-xs font-medium text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-primary data-[state=active]:bg-primary/12 data-[state=active]:text-primary',
      className,
    )} {...props} />
  )
}

export function TabsContent({ className, ...props }: ComponentProps<typeof RadixTabs.Content>) {
  return <RadixTabs.Content data-slot="tabs-content" className={cn('mt-3 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-primary', className)} {...props} />
}
