use base64::Engine;
use lingxi::qq::stickers;
use std::{borrow::Cow, io::Cursor};

fn animation(delays: &[u16], width: u16, height: u16) -> Vec<u8> {
    let palette: Vec<u8> = (0..=255u8).flat_map(|value| [value, 0, 0]).collect();
    let mut bytes = Vec::new();
    {
        let mut encoder = gif::Encoder::new(&mut bytes, width, height, &palette).unwrap();
        for (index, delay) in delays.iter().enumerate() {
            encoder
                .write_frame(&gif::Frame {
                    width: 1,
                    height: 1,
                    delay: *delay,
                    dispose: gif::DisposalMethod::Keep,
                    buffer: Cow::Owned(vec![(index % 256) as u8]),
                    ..Default::default()
                })
                .unwrap();
        }
    }
    bytes
}

fn pixel(png: &[u8], x: u32) -> [u8; 4] {
    image::load_from_memory(png)
        .unwrap()
        .into_rgba8()
        .get_pixel(x, 0)
        .0
}

#[test]
fn samples_ten_distinct_ordered_frames_across_the_whole_animation() {
    let bytes = animation(&[10; 23], 1, 1);
    let picture = stickers::prepare(bytes.clone()).unwrap();
    assert_eq!(picture.bytes, bytes);
    assert_eq!(picture.frame_count, 23);
    assert_eq!(picture.duration_ms, 2300);
    assert_eq!(picture.frames.len(), 10);
    assert_eq!(picture.frames[0].index, 0);
    assert_eq!(picture.frames[9].index, 22);
    assert_eq!(picture.preview, picture.frames[0].png);
    for pair in picture.frames.windows(2) {
        assert!(pair[0].index < pair[1].index);
        assert!(pair[0].timestamp_ms < pair[1].timestamp_ms);
    }
    for frame in &picture.frames {
        assert_eq!(frame.timestamp_ms, frame.index as u64 * 100);
        assert_eq!(pixel(&frame.png, 0), [frame.index as u8, 0, 0, 255]);
    }
    let content = picture.vision_content("查看动图");
    assert_eq!(content.len(), 21);
    assert!(
        content[0]["text"]
            .as_str()
            .unwrap()
            .contains("中间画面未知")
    );
    for (slot, frame) in picture.frames.iter().enumerate() {
        assert!(
            content[slot * 2 + 1]["text"]
                .as_str()
                .unwrap()
                .contains(&format!("时间 {} 毫秒", frame.timestamp_ms))
        );
        let url = content[slot * 2 + 2]["image_url"]["url"].as_str().unwrap();
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(url.strip_prefix("data:image/png;base64,").unwrap())
            .unwrap();
        assert_eq!(decoded, frame.png);
    }
}

#[test]
fn time_weighting_keeps_long_holds_without_duplicate_frames() {
    let mut delays = vec![1; 30];
    delays[15] = 1000;
    let picture = stickers::prepare(animation(&delays, 1, 1)).unwrap();
    let indices: Vec<_> = picture.frames.iter().map(|frame| frame.index).collect();
    assert_eq!(indices, vec![0, 15, 16, 17, 18, 19, 20, 21, 22, 29]);
    assert_eq!(picture.duration_ms, 10290);
}

#[test]
fn short_and_zero_delay_animations_keep_actual_frame_counts() {
    for count in [1, 3, 10] {
        let picture = stickers::prepare(animation(&vec![7; count], 1, 1)).unwrap();
        assert_eq!(picture.frames.len(), count);
        assert_eq!(picture.frame_count, count);
        for (index, frame) in picture.frames.iter().enumerate() {
            assert_eq!(frame.index, index);
        }
    }
    let zero = stickers::prepare(animation(&[0; 23], 1, 1)).unwrap();
    assert_eq!(zero.duration_ms, 0);
    assert_eq!(zero.frames.len(), 10);
    assert_eq!(zero.frames.first().unwrap().index, 0);
    assert_eq!(zero.frames.last().unwrap().index, 22);
    assert!(zero.frames.windows(2).all(|p| p[0].index < p[1].index));
}

#[test]
fn composites_offsets_transparency_and_restore_disposal_methods() {
    let mut bytes = Vec::new();
    {
        let palette = [0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255];
        let mut encoder = gif::Encoder::new(&mut bytes, 3, 1, &palette).unwrap();
        let frames = [
            (0, vec![1, 1, 1], gif::DisposalMethod::Keep, None),
            (1, vec![2], gif::DisposalMethod::Previous, None),
            (2, vec![3], gif::DisposalMethod::Background, None),
            (0, vec![0, 2, 0], gif::DisposalMethod::Keep, Some(0)),
        ];
        for (left, pixels, dispose, transparent) in frames {
            encoder
                .write_frame(&gif::Frame {
                    left,
                    width: pixels.len() as u16,
                    height: 1,
                    delay: 10,
                    dispose,
                    transparent,
                    buffer: Cow::Owned(pixels),
                    ..Default::default()
                })
                .unwrap();
        }
    }
    let picture = stickers::prepare(bytes).unwrap();
    let red = [255, 0, 0, 255];
    let green = [0, 255, 0, 255];
    let blue = [0, 0, 255, 255];
    assert_eq!(pixel(&picture.frames[0].png, 0), red);
    assert_eq!(pixel(&picture.frames[1].png, 0), red);
    assert_eq!(pixel(&picture.frames[1].png, 1), green);
    assert_eq!(pixel(&picture.frames[2].png, 1), red);
    assert_eq!(pixel(&picture.frames[2].png, 2), blue);
    assert_eq!(pixel(&picture.frames[3].png, 0), red);
    assert_eq!(pixel(&picture.frames[3].png, 1), green);
    assert_eq!(pixel(&picture.frames[3].png, 2), [0, 0, 0, 0]);
}

#[test]
fn rejects_corruption_and_animation_budgets_before_full_decode() {
    let bytes = animation(&[10; 20], 1, 1);
    assert!(stickers::prepare(bytes[..bytes.len() - 5].to_vec()).is_err());
    let too_many = stickers::prepare(animation(&[1; 1001], 1, 1));
    assert!(too_many.err().unwrap().to_string().contains("1000帧"));
    let too_long = stickers::prepare(animation(&[u16::MAX], 1, 1));
    assert!(too_long.err().unwrap().to_string().contains("10分钟"));
    // Tiny patch frames on a large canvas still require full-canvas compositing.
    let too_many_pixels = stickers::prepare(animation(&[1; 41], 2000, 2000));
    assert!(too_many_pixels.err().unwrap().to_string().contains("1.6亿"));
}

#[test]
fn static_images_keep_one_image_payload_and_thumbnail_dimensions() {
    let image = image::DynamicImage::ImageRgba8(image::RgbaImage::from_pixel(
        800,
        400,
        image::Rgba([40, 80, 120, 255]),
    ));
    let mut bytes = Cursor::new(Vec::new());
    image.write_to(&mut bytes, image::ImageFormat::Png).unwrap();
    let picture = stickers::prepare(bytes.into_inner()).unwrap();
    assert_eq!(picture.frame_count, 1);
    assert_eq!(picture.frames.len(), 1);
    assert_eq!(picture.duration_ms, 0);
    assert_eq!(
        image::load_from_memory(&picture.preview).unwrap().width(),
        512
    );
    assert_eq!(picture.vision_content("查看")[1]["type"], "image_url");
}

fn vision_bytes(picture: &stickers::Picture) -> (String, Vec<u8>) {
    let content = picture.vision_content("查看原图");
    let (mime, base64) = content[1]["image_url"]["url"]
        .as_str()
        .unwrap()
        .split_once(";base64,")
        .unwrap();
    (
        mime.into(),
        base64::engine::general_purpose::STANDARD
            .decode(base64)
            .unwrap(),
    )
}

#[test]
fn phone_original_over_previous_pixel_limit_is_compressed_for_vision_only() {
    let mut original = Cursor::new(Vec::new());
    image::DynamicImage::new_rgb8(5000, 4000)
        .write_to(&mut original, image::ImageFormat::Jpeg)
        .unwrap();
    let original = original.into_inner();
    let picture = stickers::prepare(original.clone()).unwrap();
    assert_eq!((picture.width, picture.height), (5000, 4000));
    assert_eq!(picture.bytes, original);
    let (mime, model_image) = vision_bytes(&picture);
    assert_eq!(mime, "data:image/jpeg");
    let decoded = image::load_from_memory(&model_image).unwrap();
    assert_eq!(decoded.width(), 1024);
    assert!(decoded.height() <= 1024);
    assert!(model_image.len() <= 384 * 1024);
    assert_ne!(model_image, picture.bytes);
    assert_eq!(
        image::load_from_memory(&picture.preview).unwrap().width(),
        512
    );
}

#[test]
fn high_entropy_and_transparent_inputs_have_bounded_previews() {
    for mode in 0..3 {
        let alpha = mode == 1;
        let mut random = 37u32;
        let pixels = image::RgbaImage::from_fn(1024, 1024, |_, _| {
            let mut channel = || {
                random = random.wrapping_mul(1664525).wrapping_add(1013904223);
                (random >> 24) as u8
            };
            image::Rgba([
                channel(),
                channel(),
                channel(),
                if alpha { 90 } else { 255 },
            ])
        });
        let image = if mode != 0 {
            image::DynamicImage::ImageRgba8(pixels)
        } else {
            image::DynamicImage::ImageRgb8(image::DynamicImage::ImageRgba8(pixels).into_rgb8())
        };
        let mut original = Cursor::new(Vec::new());
        image
            .write_to(&mut original, image::ImageFormat::Png)
            .unwrap();
        let picture = stickers::prepare(original.into_inner()).unwrap();
        let (mime, compressed) = vision_bytes(&picture);
        assert!(compressed.len() <= 384 * 1024);
        assert!(picture.preview.len() <= 384 * 1024);
        let decoded = image::load_from_memory(&compressed).unwrap();
        assert!(decoded.width() <= 1024 && decoded.height() <= 1024);
        if alpha {
            assert_eq!(mime, "data:image/png");
            assert_eq!(decoded.into_rgba8().get_pixel(0, 0).0[3], 90);
        } else {
            assert_eq!(mime, "data:image/jpeg");
        }
    }
}

#[test]
fn oversized_sources_and_gifs_still_have_separate_resource_limits() {
    let oversized = stickers::prepare(vec![0; 32 * 1024 * 1024 + 1]);
    assert!(oversized.err().unwrap().to_string().contains("32 MiB"));
    let mut gif = animation(&[10], 1, 1);
    gif.resize(8 * 1024 * 1024 + 1, 0);
    assert!(
        stickers::prepare(gif)
            .err()
            .unwrap()
            .to_string()
            .contains("8 MiB")
    );
    // Reject dimensions before allocating a decompressed image.
    let oversized_dimensions = animation(&[10], 8193, 1);
    assert!(
        stickers::prepare(oversized_dimensions)
            .err()
            .unwrap()
            .to_string()
            .contains("8192")
    );
}

#[test]
fn detailed_gif_frames_are_compressed_without_sending_original_animation() {
    let palette: Vec<u8> = (0..=255u8)
        .flat_map(|value| [value, value.rotate_left(3), value ^ 0xa5])
        .collect();
    let mut random = 37u32;
    let pixels: Vec<u8> = (0..600 * 600)
        .map(|_| {
            random = random.wrapping_mul(1664525).wrapping_add(1013904223);
            (random >> 24) as u8
        })
        .collect();
    let mut bytes = Vec::new();
    {
        let mut encoder = gif::Encoder::new(&mut bytes, 600, 600, &palette).unwrap();
        encoder
            .write_frame(&gif::Frame {
                width: 600,
                height: 600,
                delay: 10,
                buffer: Cow::Owned(pixels),
                ..Default::default()
            })
            .unwrap();
    }
    let picture = stickers::prepare(bytes.clone()).unwrap();
    assert_eq!(picture.bytes, bytes);
    assert_eq!(picture.frames.len(), 1);
    assert!(picture.frames[0].png.len() <= 384 * 1024);
    let content = picture.vision_content("查看动图");
    let url = content[2]["image_url"]["url"].as_str().unwrap();
    let frame = base64::engine::general_purpose::STANDARD
        .decode(url.strip_prefix("data:image/png;base64,").unwrap())
        .unwrap();
    assert_eq!(frame, picture.frames[0].png);
}
