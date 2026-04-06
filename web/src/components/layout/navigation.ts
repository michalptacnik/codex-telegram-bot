import type { LucideIcon } from 'lucide-react';
import {
  Activity,
  Brain,
  Clock,
  DollarSign,
  LayoutDashboard,
  MessageSquare,
  Package,
  Puzzle,
  Settings,
  Share2,
  Sparkles,
  Stethoscope,
  Target,
  Users,
  Wrench,
} from 'lucide-react';

export interface NavItem {
  to: string;
  title: string;
  icon: LucideIcon;
}

export interface NavSection {
  title: string;
  items: NavItem[];
}

// ---------------------------------------------------------------------------
// Agent-scoped navigation (shown inside /agents/:agentId/*)
// ---------------------------------------------------------------------------

export function getAgentNavSections(agentId: string, agentClass?: string): NavSection[] {
  const base = `/agents/${agentId}`;
  const sections: NavSection[] = [
    {
      title: 'Agent',
      items: [
        { to: base, icon: LayoutDashboard, title: 'Dashboard' },
        { to: `${base}/chat`, icon: MessageSquare, title: 'Chat' },
        { to: `${base}/missions`, icon: Target, title: 'Missions' },
        { to: `${base}/sessions`, icon: Users, title: 'Sessions' },
      ],
    },
    {
      title: 'Operations',
      items: [
        { to: `${base}/automations`, icon: Clock, title: 'Automations' },
        { to: `${base}/tools`, icon: Wrench, title: 'Tools' },
        { to: `${base}/memory`, icon: Brain, title: 'Memory' },
        ...(agentClass === 'social_media_manager'
          ? [{ to: `${base}/social-accounts`, icon: Share2, title: 'Social Accounts' }]
          : []),
      ],
    },
    {
      title: 'Agent Config',
      items: [
        { to: `${base}/settings`, icon: Settings, title: 'Settings' },
        { to: `${base}/soul`, icon: Sparkles, title: 'Soul' },
      ],
    },
  ];
  return sections;
}

// ---------------------------------------------------------------------------
// Global navigation (shown outside agent scope)
// ---------------------------------------------------------------------------

export const globalNavSections: NavSection[] = [
  {
    title: 'Overview',
    items: [
      { to: '/automations', icon: Clock, title: 'All Automations' },
      { to: '/integrations', icon: Puzzle, title: 'Integrations' },
      { to: '/plugins', icon: Package, title: 'Plugins' },
    ],
  },
  {
    title: 'System',
    items: [
      { to: '/settings', icon: Settings, title: 'Settings' },
      { to: '/cost', icon: DollarSign, title: 'Cost' },
      { to: '/logs', icon: Activity, title: 'Logs' },
      { to: '/doctor', icon: Stethoscope, title: 'Doctor' },
    ],
  },
];

// ---------------------------------------------------------------------------
// Legacy flat nav (kept for backward-compat with command palette actions)
// ---------------------------------------------------------------------------

export const primaryNavSections: NavSection[] = [
  {
    title: 'Workspace',
    items: [
      { to: '/', icon: Sparkles, title: 'Studio' },
      { to: '/dashboard', icon: LayoutDashboard, title: 'Dashboard' },
      { to: '/agent', icon: MessageSquare, title: 'Agent Chat' },
      { to: '/missions', icon: Target, title: 'Missions' },
      { to: '/sessions', icon: Users, title: 'Sessions' },
    ],
  },
  {
    title: 'Operations',
    items: [
      { to: '/tools', icon: Wrench, title: 'Tools' },
      { to: '/cron', icon: Clock, title: 'Automation' },
      { to: '/integrations', icon: Puzzle, title: 'Integrations' },
      { to: '/memory', icon: Brain, title: 'Memory' },
      { to: '/plugins', icon: Package, title: 'Plugins' },
    ],
  },
  {
    title: 'System',
    items: [
      { to: '/config', icon: Settings, title: 'Settings' },
      { to: '/cost', icon: DollarSign, title: 'Cost' },
      { to: '/logs', icon: Activity, title: 'Logs' },
      { to: '/doctor', icon: Stethoscope, title: 'Doctor' },
      { to: '/soul', icon: Sparkles, title: 'Soul' },
    ],
  },
];

export const navItems = primaryNavSections.flatMap((section) => section.items);

export const routeTitles: Record<string, string> = Object.fromEntries(
  navItems.map((item) => [item.to, item.title]),
);

export const menuActionRoutes: Record<string, string> = {
  'navigate.studio': '/agents',
  'navigate.dashboard': '/dashboard',
  'navigate.agent': '/agent',
  'navigate.missions': '/missions',
  'navigate.sessions': '/sessions',
  'navigate.tools': '/tools',
  'navigate.memory': '/memory',
  'navigate.logs': '/logs',
  'navigate.cost': '/cost',
  'app.preferences': '/settings',
  'help.diagnostics': '/doctor',
  'file.new_agent': '/setup',
  'file.new_mission': '/missions?new=1',
  'file.new_session': '/sessions?new=1',
};
