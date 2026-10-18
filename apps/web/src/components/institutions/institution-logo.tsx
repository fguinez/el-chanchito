"use client";

import Image from "next/image";
import { useState } from "react";
import { Building2 } from "lucide-react";
import { institutionLogo } from "@/lib/institution-logos";
import { cn } from "@/lib/utils";

const SIZES = {
  /** Page and card headers. */
  md: { px: 36, tile: "size-9 rounded-md", padding: "p-1", icon: "size-5" },
  /** Inline, before a name in rows, chips and breadcrumbs. */
  sm: { px: 16, tile: "size-4 rounded-[3px]", padding: "p-0", icon: "size-3" },
} as const;

/** An institution's logo in a square tile. The tile is white in both themes:
 *  logos assume a white background. */
export function InstitutionLogo({
  slug,
  name,
  size = "md",
  className,
  fallbackClassName,
}: {
  slug: string;
  /** Alt text; omit it where the name is printed next to the logo. */
  name?: string;
  size?: keyof typeof SIZES;
  className?: string;
  /** Classes for the fallback tile only, e.g. a hover tint that would wash
   *  out a logo's white tile. */
  fallbackClassName?: string;
}) {
  const logo = institutionLogo(slug);
  // Keyed by src so a different slug gets a fresh attempt.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const s = SIZES[size];

  if (!logo || failedSrc === logo.src) {
    return (
      <span
        className={cn(
          "flex shrink-0 items-center justify-center bg-muted",
          s.tile,
          className,
          fallbackClassName
        )}
      >
        <Building2 className={cn(s.icon, "text-muted-foreground")} />
      </span>
    );
  }

  return (
    <span
      className={cn(
        "flex shrink-0 items-center justify-center overflow-hidden",
        s.tile,
        !logo.bleed && ["border bg-white", s.padding],
        className
      )}
    >
      <Image
        // A new element per logo, so a slug change never shows the old one
        // while the new file loads.
        key={logo.src}
        src={logo.src}
        alt={name ?? ""}
        width={s.px}
        height={s.px}
        // Local files, mostly SVG: nothing for the optimizer to do.
        unoptimized
        className="size-full object-contain"
        onError={() => setFailedSrc(logo.src)}
      />
    </span>
  );
}
