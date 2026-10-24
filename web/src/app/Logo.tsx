/** A compact whale/fish mark in the visual family of the DeepSeek Harness. */
export function WhaleMark({ size = 24, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <path
        d="M2.2 12.1c2.7-4.4 6.6-6.7 11.2-6.7 4.3 0 7.6 2.2 9.4 6.4-1.8 4.4-5.1 6.8-9.7 6.8-4.3 0-7.9-2.1-10.9-6.5Z"
        fill="currentColor"
      />
      <path d="M20.2 8.2 23.2 5v11.2l-3.2-3.4V8.2Z" fill="currentColor" />
      <path
        d="M6.1 11.2c.7-.9 1.6-1.4 2.8-1.6M14.8 9.6c1.1.2 1.9.7 2.5 1.6"
        stroke="var(--logo-cut, #151517)"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <circle cx="9.4" cy="10.8" r="0.85" fill="var(--logo-cut, #151517)" />
    </svg>
  )
}
