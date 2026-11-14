import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "mapbox-gl/dist/mapbox-gl.css";
import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

const jetbrains = JetBrains_Mono({
  variable: "--font-jetbrains",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Cooldown — custom grid scenarios with governed agents",
  description:
    "Describe any grid stress test in plain language, let a Scenario Designer agent build the simulation, and audit every governed action in MongoDB Atlas.",
};

export const viewport: Viewport = {
  themeColor: "#04060a",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrains.variable} h-full antialiased`}>
      <body className="h-full overflow-hidden bg-background text-foreground">{children}</body>
    </html>
  );
}
