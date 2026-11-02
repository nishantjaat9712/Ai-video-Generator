import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "StoryOrbit — AI Film Studio", description: "Your stories, brought into motion. A real AI filmmaking workspace for Hindi and English stories." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
