import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://www.veetbot.com"),
  title: {
    default: "Veetbot | Your personal AI assistant",
    template: "%s | Veetbot",
  },
  description:
    "A personal AI assistant that helps with your inbox, your day, and the people in your life.",
  icons: {
    icon: "/veetbot-icon.svg",
    shortcut: "/veetbot-icon.svg",
  },
  openGraph: {
    type: "website",
    siteName: "Veetbot",
    title: "Veetbot | Your personal AI assistant",
    description: "An assistant that actually helps. And remembers what you told it.",
    url: "https://www.veetbot.com/",
    images: [
      {
        url: "/og.png",
        width: 1731,
        height: 909,
        alt: "Veetbot — An assistant that actually helps. And remembers what you told it.",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Veetbot | Your personal AI assistant",
    description: "An assistant that actually helps. And remembers what you told it.",
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
