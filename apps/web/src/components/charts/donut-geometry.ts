// Geometry shared by the SVG donuts (CategoryDonut, BudgetDonut): a square
// viewBox of SIZE, a ring between INNER_RADIUS and OUTER_RADIUS, angles in
// degrees counterclockwise from 3 o'clock.

export const SIZE = 208;
export const CENTER = SIZE / 2;
export const INNER_RADIUS = 64;
export const OUTER_RADIUS = 92;
/** Gap between slices, in degrees. */
export const PADDING_ANGLE = 1;

/** Start and end angle of each value's slice, proportional to its share of
 *  the sum (negatives count as 0), with PADDING_ANGLE between slices when
 *  there are several. */
export function sliceAngles(values: readonly number[]): { start: number; end: number }[] {
  const clamped = values.map((value) => Math.max(value, 0));
  const sum = clamped.reduce((a, b) => a + b, 0);
  const pad = values.length > 1 ? PADDING_ANGLE : 0;
  const sweep = 360 - values.length * pad;
  let angle = 0;
  return clamped.map((value) => {
    const start = angle;
    const end = start + (sum > 0 ? (value / sum) * sweep : 0);
    angle = end + pad;
    return { start, end };
  });
}

function polar(radius: number, angle: number): string {
  const rad = (angle * Math.PI) / 180;
  return `${CENTER + radius * Math.cos(rad)},${CENTER - radius * Math.sin(rad)}`;
}

/** Ring segment between two angles (SVG y grows down, so counterclockwise
 *  on screen is sweep-flag 0). */
export function arcPath(start: number, end: number): string {
  const span = Math.min(end - start, 359.999);
  const stop = start + span;
  const large = span > 180 ? 1 : 0;
  return [
    `M ${polar(OUTER_RADIUS, start)}`,
    `A ${OUTER_RADIUS} ${OUTER_RADIUS} 0 ${large} 0 ${polar(OUTER_RADIUS, stop)}`,
    `L ${polar(INNER_RADIUS, stop)}`,
    `A ${INNER_RADIUS} ${INNER_RADIUS} 0 ${large} 1 ${polar(INNER_RADIUS, start)}`,
    "Z",
  ].join(" ");
}
