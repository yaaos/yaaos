// app/icons.jsx — lucide-style icons, single stroke
const SVG = (p) => ({
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.7,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  width: 16, height: 16,
  ...p,
});

const Icons = {
  Dashboard: (p) => (
    <svg {...SVG(p)}>
      <rect x="3" y="3" width="7" height="9" rx="1.5"/>
      <rect x="14" y="3" width="7" height="5" rx="1.5"/>
      <rect x="14" y="12" width="7" height="9" rx="1.5"/>
      <rect x="3" y="16" width="7" height="5" rx="1.5"/>
    </svg>
  ),
  Tickets: (p) => (
    <svg {...SVG(p)}>
      <path d="M3 9a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1-2 2H5a2 2 0 0 1-2-2 2 2 0 0 0 0-4z"/>
      <path d="M13 5v4M13 13v6" />
    </svg>
  ),
  Brain: (p) => (
    <svg {...SVG(p)}>
      <path d="M9 3a3 3 0 0 0-3 3 3 3 0 0 0-3 3v1a3 3 0 0 0 1 2.2A3 3 0 0 0 3 14a3 3 0 0 0 3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3z"/>
      <path d="M15 3a3 3 0 0 1 3 3 3 3 0 0 1 3 3v1a3 3 0 0 1-1 2.2A3 3 0 0 1 21 14a3 3 0 0 1-3 3 3 3 0 0 1-3 3 3 3 0 0 1-3-3V6a3 3 0 0 1 3-3z"/>
    </svg>
  ),
  Prompt: (p) => (
    <svg {...SVG(p)}>
      <path d="M3 6h18"/>
      <path d="M3 12h12"/>
      <path d="M3 18h18"/>
      <path d="M17 10l3 2-3 2" />
    </svg>
  ),
  Repo: (p) => (
    <svg {...SVG(p)}>
      <circle cx="6" cy="5" r="2"/>
      <circle cx="6" cy="19" r="2"/>
      <circle cx="18" cy="8" r="2"/>
      <path d="M6 7v10M18 10v1a4 4 0 0 1-4 4H8" />
    </svg>
  ),
  Settings: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1A2 2 0 1 1 4.3 17l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1.1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8L4.2 7A2 2 0 1 1 7 4.3l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1.1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1A2 2 0 1 1 19.7 7l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>
    </svg>
  ),
  Search: (p) => (
    <svg {...SVG(p)}>
      <circle cx="11" cy="11" r="7"/>
      <path d="M21 21l-4.3-4.3"/>
    </svg>
  ),
  Filter: (p) => (
    <svg {...SVG(p)}>
      <path d="M3 5h18l-7 9v5l-4 2v-7L3 5z"/>
    </svg>
  ),
  Plus: (p) => (
    <svg {...SVG(p)}>
      <path d="M12 5v14M5 12h14"/>
    </svg>
  ),
  X: (p) => (
    <svg {...SVG(p)}>
      <path d="M6 6l12 12M18 6l-12 12"/>
    </svg>
  ),
  Check: (p) => (
    <svg {...SVG(p)}>
      <path d="M5 13l4 4L19 7"/>
    </svg>
  ),
  Chevron: (p) => (
    <svg {...SVG(p)}>
      <path d="M9 6l6 6-6 6"/>
    </svg>
  ),
  ChevronDown: (p) => (
    <svg {...SVG(p)}>
      <path d="M6 9l6 6 6-6"/>
    </svg>
  ),
  ChevronUp: (p) => (
    <svg {...SVG(p)}>
      <path d="M6 15l6-6 6 6"/>
    </svg>
  ),
  External: (p) => (
    <svg {...SVG(p)}>
      <path d="M14 5h5v5"/>
      <path d="M19 5L10 14"/>
      <path d="M19 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"/>
    </svg>
  ),
  GitHub: (p) => (
    <svg {...SVG(p)}>
      <path d="M9 21c-4 1-4-2-6-2.5"/>
      <path d="M15 21v-3.5a3 3 0 0 0-.8-2.3c2.7-.3 5.6-1.3 5.6-6a4.7 4.7 0 0 0-1.3-3.2 4.4 4.4 0 0 0-.1-3.2s-1-.3-3.4 1.3a11.7 11.7 0 0 0-6 0C6.7 2.5 5.7 2.8 5.7 2.8a4.4 4.4 0 0 0-.1 3.2A4.7 4.7 0 0 0 4.3 9.2c0 4.7 2.9 5.7 5.6 6a3 3 0 0 0-.8 2.3V21"/>
    </svg>
  ),
  GitPR: (p) => (
    <svg {...SVG(p)}>
      <circle cx="6" cy="6" r="2"/>
      <circle cx="6" cy="18" r="2"/>
      <circle cx="18" cy="18" r="2"/>
      <path d="M6 8v8M11 5h3a4 4 0 0 1 4 4v7"/>
      <path d="M14 2l4 3-4 3" />
    </svg>
  ),
  Pin: (p) => (
    <svg {...SVG(p)}>
      <path d="M12 17v5"/>
      <path d="M9 7.8 7.6 9.2A2 2 0 0 1 6.2 9.8H4.2L9.4 15l3.8-3.8-2.6-3.4"/>
      <path d="M19.8 4.2A2 2 0 0 0 18.4 2.8H8.4l2.6 3.4 1.4-1.4 2.4 2.4-1.4 1.4 3.4 2.6 2.4-2.4-1.4-1.4Z"/>
    </svg>
  ),
  PinOff: (p) => (
    <svg {...SVG(p)}>
      <path d="M3 3l18 18"/>
      <path d="M19.8 4.2 8.4 2.8l2 2.6-2 2 4 3.4M11 15l-1.6 1.6L4.2 11.4 6.2 9.4l3.4 2"/>
      <path d="M12 17v5"/>
    </svg>
  ),
  Sun: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="4"/>
      <path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"/>
    </svg>
  ),
  Moon: (p) => (
    <svg {...SVG(p)}>
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z"/>
    </svg>
  ),
  Sparkle: (p) => (
    <svg {...SVG(p)}>
      <path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5L12 3z"/>
    </svg>
  ),
  Bolt: (p) => (
    <svg {...SVG(p)}>
      <path d="M13 2L4 14h7l-1 8 9-12h-7z"/>
    </svg>
  ),
  AlertTri: (p) => (
    <svg {...SVG(p)}>
      <path d="M10.3 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.7 3.86a2 2 0 0 0-3.4 0z"/>
      <path d="M12 9v4M12 17h.01"/>
    </svg>
  ),
  Dot: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="3" fill="currentColor"/>
    </svg>
  ),
  Trash: (p) => (
    <svg {...SVG(p)}>
      <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
    </svg>
  ),
  Edit: (p) => (
    <svg {...SVG(p)}>
      <path d="M12 20h9"/>
      <path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>
    </svg>
  ),
  Reply: (p) => (
    <svg {...SVG(p)}>
      <path d="M9 17l-5-5 5-5"/>
      <path d="M20 18v-2a4 4 0 0 0-4-4H4"/>
    </svg>
  ),
  Replay: (p) => (
    <svg {...SVG(p)}>
      <path d="M3 12a9 9 0 1 0 3-6.7"/>
      <path d="M3 4v5h5"/>
    </svg>
  ),
  Clock: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="9"/>
      <path d="M12 7v5l3 2"/>
    </svg>
  ),
  Coin: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="9"/>
      <path d="M14.5 9.5a3 3 0 0 0-5 0c0 3 5 2 5 5a3 3 0 0 1-5 0"/>
      <path d="M12 6v2M12 16v2"/>
    </svg>
  ),
  Token: (p) => (
    <svg {...SVG(p)}>
      <path d="M4 7h16M4 12h12M4 17h16"/>
    </svg>
  ),
  Menu: (p) => (
    <svg {...SVG(p)}>
      <path d="M4 6h16M4 12h16M4 18h16"/>
    </svg>
  ),
  More: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="1.2" fill="currentColor"/>
      <circle cx="5"  cy="12" r="1.2" fill="currentColor"/>
      <circle cx="19" cy="12" r="1.2" fill="currentColor"/>
    </svg>
  ),
  Live: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="3" fill="currentColor"/>
      <path d="M16.2 7.8a6 6 0 0 1 0 8.4M7.8 7.8a6 6 0 0 0 0 8.4"/>
      <path d="M19 5a10 10 0 0 1 0 14M5 5a10 10 0 0 0 0 14"/>
    </svg>
  ),
  CheckCircle: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="9"/>
      <path d="M8 12l3 3 5-6"/>
    </svg>
  ),
  XCircle: (p) => (
    <svg {...SVG(p)}>
      <circle cx="12" cy="12" r="9"/>
      <path d="M9 9l6 6M15 9l-6 6"/>
    </svg>
  ),
  Send: (p) => (
    <svg {...SVG(p)}>
      <path d="M22 2L11 13"/>
      <path d="M22 2l-7 20-4-9-9-4 20-7z"/>
    </svg>
  ),
  Save: (p) => (
    <svg {...SVG(p)}>
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
      <path d="M17 21v-8H7v8M7 3v5h8"/>
    </svg>
  ),
  Wand: (p) => (
    <svg {...SVG(p)}>
      <path d="M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 6.2 19 5M17.8 11.8 19 13"/>
      <path d="M13 12L3 22"/>
    </svg>
  ),
};

window.Icons = Icons;
