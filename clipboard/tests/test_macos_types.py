"""Clipboard discovery must not ask Finder to materialize image representations."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
import pytest

spec=importlib.util.spec_from_file_location('bridge',Path(__file__).parents[1]/'clipboard_bridge.py')
bridge=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_finder_tiff_advertises_png_conversion_without_reading_image(monkeypatch):
    run=Mock(return_value=(0,json.dumps(['public.tiff','public.utf8-plain-text']).encode()))
    monkeypatch.setattr(bridge,'run_tool',run)
    monkeypatch.setattr(bridge.shutil,'which',lambda name: '/bin/pngpaste')
    status,types=bridge.macos_list_types()
    assert status==0
    assert types==b'image/png\nimage/tiff\ntext/plain\n'
    assert run.call_count==1
    args,kwargs=run.call_args
    assert args[0]==['osascript','-l','JavaScript','-']
    assert b'generalPasteboard.types' in kwargs['payload']
    assert b'clipboard info' not in kwargs['payload']


def test_text_file_reference_is_not_advertised_as_image(monkeypatch):
    monkeypatch.setattr(bridge,'run_tool',lambda *args,**kwargs:(0,b'["public.file-url","public.utf8-plain-text"]'))
    monkeypatch.setattr(bridge,'macos_file_urls',lambda:(0,['file:///tmp/note.txt']))
    assert bridge.macos_list_types()==(0,b'text/plain\n')


def test_no_png_converter_does_not_advertise_unreadable_png(monkeypatch):
    monkeypatch.setattr(bridge,'run_tool',lambda *args,**kwargs:(0,b'["public.tiff"]'))
    monkeypatch.setattr(bridge.shutil,'which',lambda name: None)
    assert bridge.macos_list_types()==(0,b'image/tiff\n')


@pytest.mark.parametrize('response',[b'invalid',b'null',b'{}',b'[5]'])
def test_invalid_native_metadata_fails_cleanly(monkeypatch,response):
    monkeypatch.setattr(bridge,'run_tool',lambda *args,**kwargs:(0,response))
    assert bridge.macos_list_types()==(1,b'')


def test_finder_photo_reads_file_bytes_instead_of_icon(monkeypatch,tmp_path):
    image=tmp_path/'actual photo.png'
    real=b'\x89PNG\r\n\x1a\nACTUAL_PHOTO'
    image.write_bytes(real)
    monkeypatch.setattr(bridge,'macos_file_urls',lambda:(0,[image.as_uri()]))
    run=Mock(side_effect=AssertionError('must not ask pngpaste for the file icon'))
    monkeypatch.setattr(bridge,'run_tool',run)
    assert bridge.macos_read('image/png')==(0,real)
    run.assert_not_called()


def test_non_image_file_does_not_paste_its_icon(monkeypatch,tmp_path):
    file=tmp_path/'document.txt'; file.write_text('text')
    monkeypatch.setattr(bridge,'macos_file_urls',lambda:(0,[file.as_uri()]))
    assert bridge.macos_read('image/png')==(1,b'')


def test_actual_jpeg_is_converted_and_never_reads_icon(monkeypatch,tmp_path):
    image=tmp_path/'actual photo.jpg'; image.write_bytes(b'JPEG')
    monkeypatch.setattr(bridge,'macos_file_urls',lambda:(0,[image.as_uri()]))
    def convert(args,**kwargs):
        assert args[:4]==['/usr/bin/sips','-s','format','png']
        assert args[4]==str(image)
        Path(args[-1]).write_bytes(b'\x89PNG\r\n\x1a\nPHOTO')
        return 0,b''
    monkeypatch.setattr(bridge,'run_tool',convert)
    assert bridge.macos_read('image/png')==(0,b'\x89PNG\r\n\x1a\nPHOTO')


@pytest.mark.parametrize('urls', [['https://example.com/photo.jpg'],['file://otherhost/photo.jpg'],['file:///one.jpg','file:///two.jpg']])
def test_only_one_local_file_is_accepted(urls):
    assert bridge.copied_image_path(urls) is None


def test_screenshot_without_file_url_keeps_pixel_paste(monkeypatch):
    monkeypatch.setattr(bridge,'macos_file_urls',lambda:(0,[]))
    monkeypatch.setattr(bridge.shutil,'which',lambda name:'/bin/pngpaste')
    monkeypatch.setattr(bridge,'run_tool',lambda args,**kwargs:(0,b'SCREENSHOT') if args==['pngpaste','-'] else (1,b''))
    assert bridge.macos_read('image/png')==(0,b'SCREENSHOT')


def test_failed_file_reference_query_cannot_fall_back_to_icon(monkeypatch):
    monkeypatch.setattr(bridge,'macos_file_urls',lambda:(1,[]))
    assert bridge.macos_read('image/png')==(1,b'')


def test_native_helper_avoids_applescript_when_installed(monkeypatch,tmp_path):
    helper=tmp_path/'macos-clipboard-helper'; helper.touch()
    monkeypatch.setattr(bridge,'MACOS_HELPER_PATH',helper)
    run=Mock(return_value=(0,b'["public.png"]'))
    monkeypatch.setattr(bridge,'run_tool',run)
    assert bridge.macos_metadata('types','unused script')==(0,b'["public.png"]')
    run.assert_called_once_with([str(helper),'types'])


def test_finder_advertises_actual_photo_not_icon_formats(monkeypatch,tmp_path):
    image=tmp_path/'photo.jpg'; image.write_bytes(b'JPEG')
    monkeypatch.setattr(bridge,'macos_file_urls',lambda:(0,[image.as_uri()]))
    monkeypatch.setattr(bridge.shutil,'which',lambda name:None)
    monkeypatch.setattr(bridge,'run_tool',lambda *args,**kwargs:(0,b'["public.file-url","public.tiff","public.utf8-plain-text"]'))
    # sips can convert the actual file without pngpaste. The icon's TIFF is omitted.
    assert bridge.macos_list_types()==(0,b'image/png\ntext/plain\n')
