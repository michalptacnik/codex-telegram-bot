import { Outlet } from 'react-router-dom';
import Sidebar from '@/components/layout/Sidebar';
import Header from '@/components/layout/Header';
import MacLayout from '@/components/layout/MacLayout';
import { useShell } from '@/components/shell/ShellProvider';

export default function Layout() {
  const { isDesktopMac } = useShell();

  if (isDesktopMac) {
    return <MacLayout />;
  }

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      {/* Fixed sidebar */}
      <Sidebar />

      {/* Main area offset by sidebar width (240px / w-60) */}
      <div className="ml-60 flex flex-col min-h-screen">
        <Header />

        {/* Page content */}
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
