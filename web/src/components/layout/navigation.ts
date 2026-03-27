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

export const routeTitles = Object.fromEntries(
  navItems.map((item) => [item.to, item.title]),
) as Record<string, string>;

export const menuActionRoutes: Record<string, string> = {
  'navigate.studio': '/',
  'navigate.dashboard': '/dashboard',
  'navigate.agent': '/agent',
  'navigate.missions': '/missions',
  'navigate.sessions': '/sessions',
  'navigate.tools': '/tools',
  'navigate.memory': '/memory',
  'navigate.logs': '/logs',
  'navigate.cost': '/cost',
  'app.preferences': '/config',
  'help.diagnostics': '/doctor',
  'file.new_mission': '/missions?new=1',
  'file.new_session': '/sessions?new=1',
};
