"""Bind large-class effects to the complete admitted reference inventory."""
from pathlib import Path
import host_disk_cold_classes as m


class ReferenceGuardAPI:
    def __init__(self,base,identities):
        self.base=base
        self.identities=identities
        self.last_reference_veto=None

    def __getattr__(self,name):
        return getattr(self.base,name)

    def path_is_active_privileged(self,path):
        from host_disk_relay_custody import matches
        try:
            for source,expected in self.identities.items():
                if not matches(Path(source),expected):
                    self.last_reference_veto=source
                    return True
        except OSError:
            self.last_reference_veto='unreadable_reference_source'
            return True
        return self.base.path_is_active_privileged(path)
