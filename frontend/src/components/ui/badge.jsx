import * as React from "react";
import { cva } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-sm border px-2 py-0.5 text-[11px] font-mono font-medium uppercase tracking-wide",
  {
    variants: {
      variant: {
        default: "border-seal/30 bg-seal/10 text-seal",
        muted: "border-muted-paper/25 bg-transparent text-muted-paper",
        brass: "border-brass/40 bg-brass/10 text-brass",
      },
    },
    defaultVariants: { variant: "default" },
  }
);

export function Badge({ className, variant, ...props }) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
