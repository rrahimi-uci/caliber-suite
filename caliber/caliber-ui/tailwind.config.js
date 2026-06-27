import tailwindAnimate from "tailwindcss-animate";

/** @type {import('tailwindcss').Config} */
// Tailwind palette + font-family settings track the mockup tokens in
// ../../ui-mockups/index.html so the SPA matches the design language
// the team has already iterated on.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // Dark mode is opt-in via the `dark` class on <html>, controlled by
  // useTheme() in src/components/useTheme.ts.
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      colors: {
        mlflow: {
          blue: "#2563eb",
          "blue-light": "#3b82f6",
          "blue-dark": "#1d4ed8",
        },
        surface: {
          50: "#f9fafb",
          100: "#f3f4f6",
          200: "#e5e7eb",
          300: "#d1d5db",
        },
        caliber: {
          // Full brand violet scale (1:1 with Tailwind shade numbers) so the
          // whole app shares one accent. Aliases below are kept for existing use.
          50: "#f5f3ff",
          100: "#ede9fe",
          200: "#ddd6fe",
          300: "#c4b5fd",
          400: "#a78bfa",
          500: "#8b5cf6",
          600: "#7c3aed",
          700: "#6d28d9",
          800: "#5b21b6",
          900: "#4c1d95",
          purple: "#7c3aed",
          "purple-light": "#8b5cf6",
          "purple-dark": "#6d28d9",
        },
      },
      boxShadow: {
        card: "0 1px 3px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04)",
        "card-hover": "0 8px 25px rgba(124, 58, 237, 0.10), 0 4px 10px rgba(15, 23, 42, 0.06)",
        glass: "0 8px 32px rgba(15, 23, 42, 0.08)",
        "nav-active": "0 1px 2px rgba(124, 58, 237, 0.12)",
        topbar: "0 1px 3px rgba(15, 23, 42, 0.06)",
      },
      backgroundImage: {
        "gradient-brand": "linear-gradient(135deg, #7c3aed 0%, #6366f1 50%, #3b82f6 100%)",
        "gradient-brand-subtle":
          "linear-gradient(135deg, rgba(124, 58, 237, 0.10) 0%, rgba(99, 102, 241, 0.06) 50%, rgba(59, 130, 246, 0.10) 100%)",
        "gradient-hero":
          "linear-gradient(135deg, rgba(124, 58, 237, 0.08) 0%, rgba(99, 102, 241, 0.04) 30%, rgba(59, 130, 246, 0.08) 100%)",
        "gradient-sidebar": "linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.98)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "fade-in": "fade-in 0.2s ease-out",
        "scale-in": "scale-in 0.2s ease-out",
      },
    },
  },
  plugins: [tailwindAnimate],
};
