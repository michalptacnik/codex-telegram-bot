import { NavLink } from 'react-router-dom';
import { primaryNavSections } from './navigation';

export default function Sidebar() {
  return (
    <aside className="fixed top-0 left-0 h-screen w-60 bg-gray-900 flex flex-col border-r border-gray-800">
      {/* Logo / Title */}
      <div className="flex items-center gap-2 px-5 py-5 border-b border-gray-800">
        <div className="h-8 w-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-sm">
          HQ
        </div>
        <div className="flex flex-col">
          <span className="text-lg font-semibold text-white tracking-wide leading-tight">
            Agent HQ
          </span>
          <span className="text-[10px] text-gray-500 leading-tight">
            Powered by ZeroClaw
          </span>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-4 px-3 space-y-1">
        {primaryNavSections.map((section) => (
          <div key={section.title}>
            <div className="pt-4 pb-2 px-3">
              <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                {section.title}
              </span>
            </div>
            {section.items.map(({ to, icon: Icon, title }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/' || to === '/dashboard'}
                className={({ isActive }) =>
                  [
                    'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-blue-600 text-white'
                      : 'text-gray-300 hover:bg-gray-800 hover:text-white',
                  ].join(' ')
                }
              >
                <Icon className="h-5 w-5 flex-shrink-0" />
                <span>{title}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}
