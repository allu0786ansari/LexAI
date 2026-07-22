import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Standard shadcn/ui-style class combiner: clsx for conditional classes, tailwind-merge to resolve conflicts. */
export function cn(...inputs) {
  return twMerge(clsx(inputs));
}
