
from io import BytesIO
from pathlib import Path

from modal import (
    Image,
    Mount,
    Stub,
    asgi_app,
    build,
    enter,
    gpu,
    method,
    web_endpoint,
)

image = Image.debian_slim().apt_install("git").apt_install(
        "libglib2.0-0", "libsm6", "libxrender1", "libxext6", "ffmpeg", "libgl1"
    ).pip_install('onnxruntime-gpu', extra_index_url="https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/").pip_install(
    "opencv-python",
    "Pillow~=10.1.0",
    "diffusers~=0.24.0",
    "transformers~=4.35.2",  # This is needed for `import torch`
    "accelerate~=0.25.0",  # Allows `device_map="auto"``, which allows computation of optimized device_map
    "safetensors~=0.4.1",  # Enables safetensor format as opposed to using unsafe pickle format
)

stub = Stub("stable-diffusion-xl-turbo", image=image)

with image.imports():
    import torch
    from torch import nn
    import numpy as np
    import cv2
    from diffusers import StableDiffusionInpaintPipeline
    from diffusers import DPMSolverMultistepScheduler
    from diffusers.utils import load_image
    from huggingface_hub import snapshot_download
    import PIL
    from transformers import SegformerImageProcessor, SegformerForSemanticSegmentation

@stub.cls(gpu=gpu.T4(), container_idle_timeout=240)
class Model:
    @build()
    def download_models(self):
        # Ignore files that we don't need to speed up download time.
        ignore = [
            "*.bin",
            "*.onnx_data",
            "*/diffusion_pytorch_model.safetensors",
        ]

        snapshot_download("runwayml/stable-diffusion-inpainting", ignore_patterns=ignore)
        snapshot_download("jonathandinu/face-parsing", ignore_patterns=ignore)

    @enter()
    def enter(self):
        self.pipe = StableDiffusionInpaintPipeline.from_pretrained(
        "runwayml/stable-diffusion-inpainting", torch_dtype=torch.float16
        )
        
        self.pipe.scheduler = DPMSolverMultistepScheduler.from_config(self.pipe.scheduler.config)
        self.pipe = self.pipe.to("cuda")

        # load models
        self.image_processor = SegformerImageProcessor.from_pretrained("jonathandinu/face-parsing")
        self.seg_model = SegformerForSemanticSegmentation.from_pretrained("jonathandinu/face-parsing")
        self.seg_model.to("cuda")
        self.seg_model.eval()



    @method()
    def inference(self, image_bytes, prompt):
        temp_init_image = load_image(PIL.Image.open(BytesIO(image_bytes)))
        longer_side = max(temp_init_image.size)
        horizontal_padding = (longer_side - temp_init_image.size[0]) / 2
        vertical_padding = (longer_side - temp_init_image.size[1]) / 2
        init_image = PIL.ImageOps.expand(temp_init_image, border=(int(horizontal_padding), int(vertical_padding)), fill='white')

        # run face seg inference on image
        inputs = self.image_processor(images=init_image, return_tensors="pt").to('cuda')
        outputs = self.seg_model(**inputs)
        logits = outputs.logits  # shape (batch_size, num_labels, ~height/4, ~width/4)

        # resize output to match input image dimensions
        upsampled_logits = nn.functional.interpolate(logits,
                        size=init_image.size[::-1], # H x W
                        mode='bilinear',
                        align_corners=False)

        # get label masks
        labels = upsampled_logits.argmax(dim=1)[0]

        parsing = labels.cpu().numpy()

        mask_image = np.zeros(parsing.shape)
        mask_image[parsing==0] = 1.0
        mask_image[parsing==18] = 1.0
        mask_image[parsing==17] = 1.0

        mask_image = mask_image

        mask_area = np.sum(mask_image)
        
        # mask_area_ratio > 0.4 then add padding around the image and resize to 512.
        total_area = np.prod(mask_image.shape)

        mask_area_ratio = mask_area / total_area

        if mask_area_ratio > 0.3:
            # Add padding around the image
            pad = int(((mask_area_ratio * 400) - 100) * 5.12)//2
            mask_image = np.pad(mask_image, ((pad, pad), (pad, pad)), mode='constant',constant_values=(1))
            mask_image = cv2.resize(mask_image, (512, 512), interpolation=cv2.INTER_AREA)
            init_image = PIL.ImageOps.expand(init_image, border=(pad, pad), fill='white')
            init_image = init_image.resize((512, 512))

        # Define the kernel size for erosion
        #kernel_size = 15  # You can adjust this value to control the amount of erosion

        # Define the kernel
        
        #kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
        
        # Apply erosion to the mask_image
        #eroded_mask = cv2.erode(mask_image, kernel, iterations=1)

        #eroded_mask = 255 *  eroded_mask
        #eroded_mask = eroded_mask.astype(np.uint8)

        mask_image = 255 * mask_image
        mask_image = mask_image.astype(np.uint8)

        mask = PIL.Image.fromarray(mask_image)

        num_inference_steps = 20
        #strength = 0.9
        # "When using SDXL-Turbo for image-to-image generation, make sure that num_inference_steps * strength is larger or equal to 1"
        # See: https://huggingface.co/stabilityai/sdxl-turbo
        #assert num_inference_steps * strength >= 1
        image = self.pipe(
            prompt,
            image=init_image,
            mask_image=mask,
            num_inference_steps=num_inference_steps,
            # strength=strength,
            # guidance_scale=0.0,
        ).images[0]



        byte_stream = BytesIO()
        image.save(byte_stream, format="JPEG")
        image_bytes = byte_stream.getvalue()

        return image_bytes


DEFAULT_IMAGE_PATH = "/home/karan/kj_workspace/kj_ai/Professional-Headshot-with-single-image/backend/modal_inference/akhil.png"


@stub.local_entrypoint()
def main(
    image_path=DEFAULT_IMAGE_PATH,
    prompt="a person in suit, high resolution, looking towards camera, white wall background",
):
    with open(image_path, "rb") as image_file:
        input_image_bytes = image_file.read()
        output_image_bytes = Model().inference.remote(input_image_bytes, prompt)

    dir = Path("/tmp/stable-diffusion-xl-turbo")
    if not dir.exists():
        dir.mkdir(exist_ok=True, parents=True)

    output_path = dir / "output.png"
    print(f"Saving it to {output_path}")
    with open(output_path, "wb") as f:
        f.write(output_image_bytes)




web_image = Image.debian_slim().pip_install("jinja2")


@stub.function(
    image=web_image,
    allow_concurrent_inputs=20,
)
@asgi_app()
def app():

    from fastapi import FastAPI, UploadFile, File
    from fastapi.responses import StreamingResponse
    

    web_app = FastAPI()

    @web_app.post('/upload')
    async def upload_image(file: UploadFile = File(...)):

        # Read the uploaded image file
        input_image_bytes = file.file.read()
        prompt = "a person in suit, high resolution, looking towards camera, white wall background"

        output_image_bytes = Model().inference.remote(input_image_bytes, prompt)


        return StreamingResponse(BytesIO(output_image_bytes), media_type="image/png")





    return web_app