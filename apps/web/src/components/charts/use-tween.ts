"use client";

import { useEffect, useState } from "react";

/** Length of a chart transition, shared by every animated part of a chart. */
export const TRANSITION_MS = 500;
/** CSS twin of `ease` for Web Animations driven next to a tween. */
export const TRANSITION_EASING = "cubic-bezier(0.65, 0, 0.35, 1)";

/** Ease-in-out cubic. */
export function ease(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

export function lerp(from: number, to: number, t: number): number {
  return from + (to - from) * t;
}

export function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * Animates toward `target` whenever `signature` changes, returning
 * `interpolate(from, to, easedProgress)` on every frame. `from` is whatever
 * was on screen when the change landed, so an interrupted transition carries
 * on from there. At rest, and when the viewer prefers reduced motion, it
 * returns `target` itself.
 */
export function useTween<S>(
  target: S,
  signature: string,
  interpolate: (from: S, to: S, t: number) => S
): S {
  const [state, setState] = useState({ signature, from: target, to: target, t: 1 });
  let current = state;
  if (state.signature !== signature) {
    current = {
      signature,
      from: interpolate(state.from, state.to, ease(state.t)),
      to: target,
      t: prefersReducedMotion() ? 1 : 0,
    };
    setState(current);
  }

  const animating = current.t < 1;
  useEffect(() => {
    if (!animating) return;
    const start = performance.now();
    let frame = requestAnimationFrame(function step(now) {
      const t = Math.min((now - start) / TRANSITION_MS, 1);
      setState((s) => (s.signature === signature ? { ...s, t } : s));
      if (t < 1) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  }, [signature, animating]);

  return current.t >= 1 ? target : interpolate(current.from, current.to, ease(current.t));
}
