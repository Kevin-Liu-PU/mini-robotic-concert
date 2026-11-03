"""RAM-only chin-line preview. Saves numeric JSON only after explicit Enter."""
import argparse
import json
from pathlib import Path
from conductor_rt.camera_setup import camera_backend,configure_camera,prepare_frame,make_calibration

WINDOW='Chin calibration - RAM preview only'

class Selection:
    def __init__(self,width,height):
        self.width,self.height=width,height
        self.y=None
    def click(self,x,y):
        if type(x) is int and type(y) is int and 0<=x<self.width and 0<y<self.height:
            self.y=y
            return True
        return False
    @property
    def chin_y(self):return None if self.y is None else self.y/self.height

def calibrate(cv2,camera,output,width=640):
    output=Path(output)
    if camera<0 or not 256<=width<=1920:raise ValueError('Invalid camera index or processing width.')
    if output.exists():raise FileExistsError('Calibration output already exists; choose a new filename.')
    if not output.parent.is_dir():raise ValueError('Output parent directory must already exist.')
    cap=None;window=False;selection=None;capture_size=None
    backend,_=camera_backend(cv2)
    try:
        cap=cv2.VideoCapture(camera,backend)
        if not cap.isOpened():raise RuntimeError('Camera could not be opened; no calibration was saved.')
        configure_camera(cv2,cap,width)
        while True:
            ok,image=cap.read()
            if not ok:raise RuntimeError('Camera read failed; no calibration was saved.')
            size=(int(image.shape[1]),int(image.shape[0]))
            if capture_size is not None and capture_size!=size:
                raise RuntimeError('Frame dimensions changed during calibration; repeat calibration.')
            capture_size=size
            preview=prepare_frame(cv2,image,width)
            if selection is None:
                selection=Selection(int(preview.shape[1]),int(preview.shape[0]))
                cv2.namedWindow(WINDOW,cv2.WINDOW_AUTOSIZE);window=True
                def mouse(event,x,y,flags,param):
                    if event==cv2.EVENT_LBUTTONDOWN:selection.click(x,y)
                cv2.setMouseCallback(WINDOW,mouse)
            if selection.y is not None:
                cv2.line(preview,(0,selection.y),(selection.width-1,selection.y),(0,255,0),1)
            value='Click the lowest point of your chin' if selection.y is None else f'chin_y={selection.chin_y:.8f}  Click again to adjust'
            cv2.putText(preview,value,(8,22),cv2.FONT_HERSHEY_SIMPLEX,.48,(0,255,0),1)
            cv2.putText(preview,'Enter: save number only  Esc: cancel',(8,44),cv2.FONT_HERSHEY_SIMPLEX,.48,(0,255,0),1)
            shown_y=selection.y
            cv2.imshow(WINDOW,preview)
            key=cv2.waitKey(10)&0xff
            if key==27:return None
            if key in (10,13) and selection.chin_y is not None and selection.y==shown_y:
                data=make_calibration(selection.chin_y,camera,backend,*capture_size,width)
                # Exclusive create: never overwrite an earlier calibration.
                with output.open('x',encoding='utf-8') as f:
                    f.write(json.dumps(data,indent=2,allow_nan=False)+'\n')
                return data
            try:
                if cv2.getWindowProperty(WINDOW,cv2.WND_PROP_VISIBLE)<1:return None
            except Exception:
                return None
    finally:
        try:
            if cap is not None:cap.release()
        finally:
            if window:
                try:cv2.destroyWindow(WINDOW)
                except Exception:pass  # The user may have already closed it.

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--camera',type=int,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--width',type=int,default=640)
    a=p.parse_args()
    if a.camera<0 or not 256<=a.width<=1920:p.error('Invalid camera index or processing width; no camera was opened.')
    if a.output.exists() or not a.output.parent.is_dir():p.error('Choose a new output filename in an existing directory; no camera was opened.')
    import cv2
    try:result=calibrate(cv2,a.camera,a.output,a.width)
    except (ValueError,RuntimeError,OSError) as exc:
        print(str(exc));return 1
    if result is None:print('Cancelled; no calibration saved.')
    else:print(f'Saved numeric calibration to {a.output}. --chin-y {result["chin_y"]:.8f}')
    return 0

if __name__=='__main__':raise SystemExit(main())
