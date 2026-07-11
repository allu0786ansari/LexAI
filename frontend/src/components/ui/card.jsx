import * as React from "react";
import { cn } from "@/lib/utils";

export const Card = React.forwardRef(({ className, ...props }, ref) => (
  <div ref={ref} className={cn("rounded-lg border border-ink-3/10 bg-paper text-text-paper shadow-sm", className)} {...props} />
));
Card.displayName = "Card";
