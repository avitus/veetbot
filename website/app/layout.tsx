import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://www.veetbot.com"),
  title: {
    default: "Veetbot | Governed AI agent",
    template: "%s | Veetbot",
  },
  description:
    "A self-hostable AI agent for durable work, governed actions, and inspectable memory.",
  icons: {
    icon: "/veetbot-icon.svg",
    shortcut: "/veetbot-icon.svg",
  },
  openGraph: {
    type: "website",
    siteName: "Veetbot",
    title: "Veetbot | Governed AI agent",
    description: "An agent that can act. A system you can inspect.",
    url: "https://www.veetbot.com/",
    images: [
      {
        url: "/og.png",
        width: 1200,
        height: 630,
        alt: "Veetbot — An agent that can act. A system you can inspect.",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Veetbot | Governed AI agent",
    description: "An agent that can act. A system you can inspect.",
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
