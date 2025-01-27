import os
import subprocess
import cv2
import copy
import random
from contextlib import closing
from PIL import Image
import modules.shared as shared
from modules.processing import StableDiffusionProcessingImg2Img, process_images
from modules.shared import opts
from modules.ui import plaintext_to_html
from modules.images import save_image
from replacer.mask_creator import MasksCreator
from replacer.generation_args import GenerationArgs
from replacer.options import ( getDetectionPromptExamples, getPositivePromptExamples,
    getNegativePromptExamples, useFirstPositivePromptFromExamples, useFirstNegativePromptFromExamples,
    getHiresFixPositivePromptSuffixExamples, EXT_NAME, EXT_NAME_LOWER, getSaveDir, needAutoUnloadModels
)

g_clear_cache = None

def clearCache():
    global g_clear_cache
    if g_clear_cache is None:
        from scripts.sam import clear_cache
        g_clear_cache = clear_cache
    g_clear_cache()




def inpaint(
    image : Image,
    gArgs : GenerationArgs,
    savePath : str = "",
    saveSuffix : str = "",
    save_to_dirs : bool = True
):
    override_settings = {}
    if (gArgs.upscalerForImg2Img is not None and gArgs.upscalerForImg2Img != ""):
        override_settings["upscaler_for_img2img"] = gArgs.upscalerForImg2Img
    if gArgs.img2img_fix_steps is not None and gArgs.img2img_fix_steps != "":
        override_settings["img2img_fix_steps"] = gArgs.img2img_fix_steps

    inpainting_fill = gArgs.inpainting_fill
    if (inpainting_fill == 4): # lama cleaner (https://github.com/light-and-ray/sd-webui-lama-cleaner-masked-content)
        inpainting_fill = 1 # original
        try:
            from lama_cleaner_masked_content.inpaint import lamaInpaint
            from lama_cleaner_masked_content.options import getUpscaler
            image = lamaInpaint(image, gArgs.mask, gArgs.inpainting_mask_invert, getUpscaler())
        except Exception as e:
            print(f'[{EXT_NAME}]: {e}')

    p = StableDiffusionProcessingImg2Img(
        sd_model=shared.sd_model,
        outpath_samples=opts.outdir_samples or opts.outdir_img2img_samples,
        outpath_grids=opts.outdir_grids or opts.outdir_img2img_grids,
        prompt=gArgs.positvePrompt,
        negative_prompt=gArgs.negativePrompt,
        styles=[],
        sampler_name=gArgs.sampler_name,
        batch_size=gArgs.batch_size,
        n_iter=gArgs.n_iter,
        steps=gArgs.steps,
        cfg_scale=gArgs.cfg_scale,
        width=gArgs.width,
        height=gArgs.height,
        init_images=[image],
        mask=gArgs.mask,
        mask_blur=gArgs.mask_blur,
        inpainting_fill=inpainting_fill,
        resize_mode=0,
        denoising_strength=gArgs.denoising_strength,
        image_cfg_scale=1.5,
        inpaint_full_res=True,
        inpaint_full_res_padding=gArgs.inpaint_full_res_padding,
        inpainting_mask_invert=gArgs.inpainting_mask_invert,
        override_settings=override_settings,
        do_not_save_samples=True,
    )

    p.extra_generation_params["Mask blur"] = gArgs.mask_blur
    p.extra_generation_params["Detection prompt"] = gArgs.detectionPrompt
    is_batch = (gArgs.n_iter > 1 or gArgs.batch_size > 1)
    p.seed = gArgs.seed
    p.do_not_save_grid = not gArgs.save_grid
    


    with closing(p):
        processed = process_images(p)

    generation_info_js = processed.js()


    if savePath != "":
        for imageToSave in processed.images:
            save_image(imageToSave, savePath, "", gArgs.seed, gArgs.positvePrompt, opts.samples_format,
                    info=processed.info, p=p, suffix=saveSuffix, save_to_dirs=save_to_dirs)

    if opts.do_not_show_images:
        processed.images = []

    return processed.images, generation_info_js, plaintext_to_html(processed.info), plaintext_to_html(processed.comments, classname="comments")



lastGenerationArgs = None

def getLastUsedSeed():
    if lastGenerationArgs is None:
        return -1
    else:
        return lastGenerationArgs.seed



def generateSingle(
    image : Image,
    gArgs : GenerationArgs,
    savePath : str,
    saveSuffix : str,
    save_to_dirs : bool,
    extra_includes : list,
):
    masksCreator = MasksCreator(gArgs.detectionPrompt, gArgs.avoidancePrompt, image, gArgs.samModel,
        gArgs.grdinoModel, gArgs.boxThreshold, gArgs.maskExpand, gArgs.maxResolutionOnDetection)

    maskNum = gArgs.seed % len(masksCreator.previews)

    maskPreview = masksCreator.previews[maskNum]
    gArgs.mask = masksCreator.masks[maskNum]
    maskCutted = masksCreator.cutted[maskNum]
    maskBox = masksCreator.boxes[maskNum]
    shared.state.assign_current_image(maskPreview)
    shared.state.textinfo = "inpaint"

    resultImages, generation_info_js, processed_info, processed_comments = \
        inpaint(image, gArgs, savePath, saveSuffix, save_to_dirs)
    
    if "mask" in extra_includes:
        resultImages.append(gArgs.mask)
    if "box" in extra_includes:
        resultImages.append(maskBox)
    if "cutted" in extra_includes:
        resultImages.append(maskCutted)
    if "preview" in extra_includes:
        resultImages.append(maskPreview)

    return resultImages, generation_info_js, processed_info, processed_comments



def generate(
    detectionPrompt: str,
    avoidancePrompt: str,
    positvePrompt: str,
    negativePrompt: str,
    tab_index,
    image_single,
    image_batch,
    input_batch_dir,
    output_batch_dir,
    show_batch_dir_results,
    input_batch_video,
    input_batch_video_fps,
    upscalerForImg2Img,
    seed,
    sampler,
    steps,
    box_threshold,
    mask_expand,
    mask_blur,
    max_resolution_on_detection,
    sam_model_name,
    dino_model_name,
    cfg_scale,
    denoise,
    inpaint_padding,
    inpainting_fill,
    width,
    batch_count,
    height,
    batch_size,
    inpainting_mask_invert,
    save_grid,
    extra_includes,
):
    global fps_in, fps_out
    shared.state.begin(job=EXT_NAME_LOWER)
    shared.total_tqdm.clear()

    if detectionPrompt == '':
        detectionPrompt = getDetectionPromptExamples()[0]
    detectionPrompt = detectionPrompt.strip()

    if positvePrompt == '' and useFirstPositivePromptFromExamples():
        positvePrompt = getPositivePromptExamples()[0]

    if negativePrompt == '' and useFirstNegativePromptFromExamples():
        negativePrompt = getNegativePromptExamples()[0]

    if (seed == -1):
        seed = int(random.randrange(4294967294))

    avoidancePrompt = avoidancePrompt.strip()

    images = []
    print(tab_index)
    if tab_index == 0:
        images = [image_single]
        generationsN = 1


    if tab_index == 1:
        def getImages(image_folder):
            for img in image_folder:
                if isinstance(img, Image.Image):
                    image = img
                else:
                    image = Image.open(os.path.abspath(img.name)).convert('RGBA')
                yield image
        images = getImages(image_batch)
        generationsN = len(image_batch)


    if tab_index == 2:
        print("wtf",input_batch_dir)
        def readImages(input_dir):
            assert not shared.cmd_opts.hide_ui_dir_config, '--hide-ui-dir-config option must be disabled'
            assert input_dir, 'input directory not selected'

            image_list = shared.listfiles(input_dir)
            for filename in image_list:
                try:
                    image = Image.open(filename).convert('RGBA')
                except Exception:
                    continue
                yield image
        images = readImages(input_batch_dir)
        print("wtffff")
        generationsN = len(shared.listfiles(input_batch_dir))

    fps_in = 0
    fps_out = 0
    if tab_index == 3:
        print("video",input_batch_dir)
        def separate_video_into_frames(video_path, fps, temp_folder):
            global fps_in, fps_out
            assert video_path, 'video not selected'
            assert temp_folder, 'temp folder not specified'

            # Create the temporary folder if it doesn't exist
            os.makedirs(temp_folder, exist_ok=True)

            # Open the video file
            video = cv2.VideoCapture(video_path)
            fps_in = video.get(cv2.CAP_PROP_FPS)
            fps_out = fps
            print(fps_in, fps_out)
            
            index_in = -1
            index_out = -1

            # Read frames from the video and save them as images
            frame_count = 0
            while True:
                success = video.grab()
                if not success: break
                index_in += 1

                out_due = int(index_in / fps_in * fps_out)
                print(index_in, out_due, index_out)
                if out_due > index_out:
                    success, frame = video.retrieve()
                    if not success: break
                    index_out += 1
                    # Save the frame as an image in the temporary folder
                    frame_path = os.path.join(temp_folder, f"frame_{frame_count}.jpg")
                    cv2.imwrite(frame_path, frame)

                    frame_count += 1

            # Release the video file
            video.release()
        def readImages(input_dir):
            assert not shared.cmd_opts.hide_ui_dir_config, '--hide-ui-dir-config option must be disabled'
            assert input_dir, 'input directory not selected'

            image_list = shared.listfiles(input_dir)
            for filename in image_list:
                try:
                    image = Image.open(filename).convert('RGBA')
                except Exception:
                    continue
                yield image
        def getVideoFrames(video_path, fps):
            assert video_path, 'video not selected'
            assert fps, 'fps not specified'
            temp_folder = os.path.join(os.path.dirname(video_path), 'temp')
            if os.path.exists(temp_folder):
                for file in os.listdir(temp_folder):
                    os.remove(os.path.join(temp_folder, file))
            separate_video_into_frames(video_path, fps, temp_folder)
            return readImages(temp_folder)
        video_batch_path = input_batch_video
        temp_batch_folder = os.path.join(os.path.dirname(video_batch_path), 'temp')
        images = getVideoFrames(video_batch_path, input_batch_video_fps)
        generationsN = len(shared.listfiles(temp_batch_folder))
        output_batch_dir = output_batch_dir + f'_{seed}'
            

    shared.state.job_count = generationsN*batch_count

    img2img_fix_steps = False

    gArgs = GenerationArgs(
        positvePrompt,
        negativePrompt,
        detectionPrompt,
        avoidancePrompt,
        None,
        upscalerForImg2Img,
        seed,
        sam_model_name,
        dino_model_name,
        box_threshold,
        mask_expand,
        max_resolution_on_detection,
        
        steps,
        sampler,
        mask_blur,
        inpainting_fill,
        batch_count,
        batch_size,
        cfg_scale,
        denoise,
        height,
        width,
        inpaint_padding,
        img2img_fix_steps,
        inpainting_mask_invert,

        images,
        generationsN,
        save_grid,
        )

    resultImages = []
    generation_info_js = ""
    processed_info = ""
    processed_comments = ""
    i = 1
    n = generationsN

    for image in images:
        if shared.state.interrupted:
            if needAutoUnloadModels():
                clearCache()
            break
        
        progressInfo = "Generate mask"
        if n > 1: 
            print(flush=True)
            print()
            print(f'    [{EXT_NAME}]    processing {i}/{n}')
            progressInfo += f" {i}/{n}"

        shared.state.textinfo = progressInfo
        shared.state.skipped = False

        saveDir = ""
        save_to_dirs = True
        if tab_index == 2 or tab_index == 3:
            saveDir = output_batch_dir
            save_to_dirs = False
        else:
            saveDir = getSaveDir()

        try:
            newImages, generation_info_js, processed_info, processed_comments = \
                    generateSingle(image, gArgs, saveDir, "", save_to_dirs, extra_includes)
        except Exception as e:
            print(f'    [{EXT_NAME}]    Exception: {e}')
            i += 1
            if needAutoUnloadModels():
                clearCache()
            if generationsN == 1:
                raise
            shared.state.nextjob()
            continue

        if not ((tab_index == 2 or tab_index == 3) and not show_batch_dir_results):
            resultImages += newImages

        i += 1

    print(resultImages)

    if tab_index == 1:
        gArgs.images = getImages(image_batch)
    if tab_index == 2:
        gArgs.images = readImages(input_batch_dir)
    print("generate done, generating video")
    if tab_index == 3:
        def generate_video(frames_dir, frames_fps, org_video, output_path, target_fps):
            ffmpeg_cmd = [
                'ffmpeg',
                '-framerate', str(frames_fps),
                '-i', os.path.join(frames_dir, '%5d-' + f'{seed}' + '.png'),
                '-r', str(frames_fps),
                '-i', org_video,
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-c:v', 'libx264',
                '-c:a', 'aac',
                '-vf', f'fps={target_fps}',
                '-shortest',
                '-y',
                output_path
            ]
            print(' '.join(str(v) for v in ffmpeg_cmd))
            subprocess.run(ffmpeg_cmd)
        # Example usage
        output_path = os.path.join(output_batch_dir, f'output_{os.path.basename(input_batch_video)}_{seed}.mp4')
        generate_video(output_batch_dir, fps_out, input_batch_video, output_path, fps_in)

        
    global lastGenerationArgs
    lastGenerationArgs = gArgs
    shared.state.end()

    return resultImages, generation_info_js, processed_info, processed_comments





def applyHiresFixSingle(
    image : Image,
    gArgs : GenerationArgs,
    hrArgs : GenerationArgs,
    saveDir : str,
):
    shared.state.textinfo = "inpaint with upscaler"
    generatedImages, _, _, _ = inpaint(image, gArgs)

    resultImages = []
    generation_info_js = ""
    processed_info = ""
    processed_comments = ""
    n = len(generatedImages)
    if n > 1: 
        print(f'    [{EXT_NAME}]    hiresfix batch count*size {n} for single image')

    for generatedImage in generatedImages:
        shared.state.textinfo = "hiresfix"
        newImages, generation_info_js, processed_info, processed_comments = \
            inpaint(generatedImage, hrArgs, saveDir, "-hires-fix")
        resultImages += newImages

    return resultImages, generation_info_js, processed_info, processed_comments




def applyHiresFix(
    hf_upscaler,
    hf_steps,
    hf_sampler,
    hf_denoise,
    hf_cfg_scale,
    hfPositivePromptSuffix,
    hf_size_limit,
    hf_above_limit_upscaler,
    hf_unload_detection_models,
):
    shared.state.begin(job=f'{EXT_NAME_LOWER}_hf')
    shared.state.job_count = 2
    shared.total_tqdm.clear()

    if hfPositivePromptSuffix == "":
        hfPositivePromptSuffix = getHiresFixPositivePromptSuffixExamples()[0]


    global lastGenerationArgs
    if lastGenerationArgs is None:
        return [], "", "", ""

    gArgs = copy.copy(lastGenerationArgs)
    gArgs.upscalerForImg2Img = hf_upscaler

    hrArgs = copy.copy(lastGenerationArgs)
    hrArgs.cfg_scale = hf_cfg_scale
    hrArgs.denoising_strength = hf_denoise
    if not hf_sampler == 'Use same sampler':
        hrArgs.sampler_name = hf_sampler
    if hf_steps != 0:
        hrArgs.steps = hf_steps
    hrArgs.positvePrompt = gArgs.positvePrompt + " " + hfPositivePromptSuffix
    hrArgs.inpainting_fill = 1 # Original
    hrArgs.img2img_fix_steps = True

    if gArgs.generationsN > 1 or gArgs.batch_size > 1 or gArgs.n_iter > 1:
        errorText = f"    [{EXT_NAME}]    applyHiresFix is not supported for batch"
        print(errorText)
        return None, "", errorText, ""

    resultImages = []
    generation_info_js = ""
    processed_info = ""
    processed_comments = ""

    if hf_unload_detection_models:
        clearCache()

    for image in gArgs.images:
        saveDir = getSaveDir()
        hrArgs.height, hrArgs.width = image.size
        if hrArgs.height > hf_size_limit:
            hrArgs.height = hf_size_limit
            hrArgs.upscalerForImg2Img = hf_above_limit_upscaler
        if hrArgs.width > hf_size_limit:
            hrArgs.width = hf_size_limit
            hrArgs.upscalerForImg2Img = hf_above_limit_upscaler

        resultImages, generation_info_js, processed_info, processed_comments = \
            applyHiresFixSingle(image, gArgs, hrArgs, saveDir)

    shared.state.end()

    return resultImages, generation_info_js, processed_info, processed_comments


def generate_webui(id_task, *args, **kwargs):
    return generate(*args, **kwargs)

def applyHiresFix_webui(id_task, *args, **kwargs):
    return applyHiresFix(*args, **kwargs)
