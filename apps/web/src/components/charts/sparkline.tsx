// A tiny bar sparkline (one bar per value, oldest first) for table cells.
// Plain SVG: a chart library per table row would be far heavier.

export function Sparkline({
  values,
  color,
  label,
  width = 64,
  height = 20,
}: {
  values: number[];
  color: string;
  /** Read by screen readers in place of the bars. */
  label: string;
  width?: number;
  height?: number;
}) {
  const max = Math.max(0, ...values);
  const step = values.length > 0 ? width / values.length : width;
  const barWidth = Math.max(1, step - 2);
  return (
    <svg
      role="img"
      aria-label={label}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="inline-block align-middle"
    >
      {values.map((value, i) => {
        // Empty months keep a 1px baseline so the month still reads.
        const h = max > 0 ? Math.max(1, (value / max) * height) : 1;
        return (
          <rect
            key={i}
            x={i * step + (step - barWidth) / 2}
            y={height - h}
            width={barWidth}
            height={h}
            rx={1}
            fill={color}
            fillOpacity={value > 0 ? 1 : 0.25}
          />
        );
      })}
    </svg>
  );
}
