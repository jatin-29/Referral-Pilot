/** Tailwind config for the dashboard. Build: scripts/build_css.sh */
module.exports = {
  content: [
    "./referralpilot/ui/templates/**/*.html",
    "./referralpilot/ui/static/app.js",
    "./referralpilot/ui/*.py",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
    },
  },
  plugins: [],
};
