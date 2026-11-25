// Rebuild static/tailwind.css after editing index.html classes:
//   npx tailwindcss@3.4.16 -c static/tailwind.config.js -i static/tailwind.src.css -o static/tailwind.css --minify
module.exports = {
  content: ['./index.html'],
  darkMode: 'class',
  theme: { extend: { colors: {
    ink: { 950: '#0b0f17', 900: '#111827', 850: '#151c2b', 800: '#1b2436', 700: '#28324a', 600: '#3a4560' },
    accent: { 400: '#7dd3fc', 500: '#38bdf8', 600: '#0ea5e9' } } } },
};
