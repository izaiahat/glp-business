import shutil
import time
import host_disk_cold_classes as m
import host_disk_large_classes as large
import host_disk_report_caches as caches


class API:
    def path_is_active_privileged(self,p):return False
    def remove_disposable(self,p,deadline):shutil.rmtree(p);return True


def test_old_report_cache_is_regenerable_but_profile_db_survives(tmp_path,monkeypatch):
    reports=tmp_path/'reports';profile=reports/'ops/old/browser-profiles/Default'
    cache=profile/'Cache';cache.mkdir(parents=True)
    (cache/'blob').write_bytes(b'x'*8192);(profile/'Cookies').write_bytes(b'db')
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+3*86400)
    import host_disk_custody
    monkeypatch.setattr(host_disk_custody,'collect',lambda:set())
    monkeypatch.setattr(m,'live_symlink_targets',lambda:set())
    monkeypatch.setattr(large,'lsof_inactive',lambda p:True)
    rows=caches.run(API(),m,reports,pins=(),dry_run=False,deadline=time.monotonic()+60)
    assert rows[0]['status']=='REMOVED' and rows[0]['bytes_freed']>0
    assert not cache.exists() and (profile/'Cookies').exists()


def test_referenced_cache_and_extensionless_database_are_held(tmp_path,monkeypatch):
    reports=tmp_path/'reports';cache=reports/'old/browser-profiles/Default/Cache'
    cache.mkdir(parents=True);(cache/'data').write_bytes(b'SQLite format 3\0'+b'x'*100)
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+3*86400)
    import host_disk_custody
    monkeypatch.setattr(host_disk_custody,'collect',lambda:set())
    monkeypatch.setattr(m,'live_symlink_targets',lambda:set())
    monkeypatch.setattr(large,'lsof_inactive',lambda p:True)
    rows=caches.run(API(),m,reports,pins=(),dry_run=False,deadline=time.monotonic()+60)
    assert rows[0]['status']=='REFUSED' and cache.exists()
    rows=caches.run(API(),m,reports,pins=(cache,),dry_run=False,deadline=time.monotonic()+60)
    assert not rows or rows[0]['status']=='REFUSED'


def test_default_directory_alone_is_not_a_browser_profile(tmp_path,monkeypatch):
    reports=tmp_path/'reports';cache=reports/'ordinary/Default/Cache'
    cache.mkdir(parents=True);(cache/'blob').write_bytes(b'evidence')
    import host_disk_custody
    monkeypatch.setattr(host_disk_custody,'collect',lambda:set())
    monkeypatch.setattr(m,'live_symlink_targets',lambda:set())
    rows=caches.run(API(),m,reports,pins=(),dry_run=False,deadline=time.monotonic()+60)
    assert rows==[] and cache.exists()


def test_live_profile_owner_vetoes_cache_removal(tmp_path,monkeypatch):
    reports=tmp_path/'reports';cache=reports/'old/browser-profiles/Default/Cache'
    cache.mkdir(parents=True);(cache/'blob').write_bytes(b'x'*4096)
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+3*86400)
    import host_disk_custody
    monkeypatch.setattr(host_disk_custody,'collect',lambda:set())
    monkeypatch.setattr(m,'live_symlink_targets',lambda:set())
    class LiveAPI(API):
        def path_is_active_privileged(self,p):return p.name=='browser-profiles'
    rows=caches.run(LiveAPI(),m,reports,pins=(),dry_run=False,deadline=time.monotonic()+60)
    assert rows[0]['status']=='REFUSED' and cache.exists()
