use std::{env, fs, path::Path};

fn main() {
    let root = Path::new(&env::var("CARGO_MANIFEST_DIR").unwrap()).join("../web/dist");
    println!("cargo:rerun-if-changed={}", root.display());
    assert!(
        root.join("index.html").is_file(),
        "Vue frontend is missing. Run `npm ci --prefix web` and `npm run build --prefix web` before building Rust, or use Docker."
    );
    let mut files = Vec::new();
    collect(&root, &mut files);
    files.sort();
    let mut source = String::from(
        "pub fn asset(path: &str) -> Option<(&'static str, &'static [u8])> { match path {\n",
    );
    for file in files {
        let relative = file
            .strip_prefix(&root)
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        let mime = match file.extension().and_then(|v| v.to_str()).unwrap_or("") {
            "html" => "text/html; charset=utf-8",
            "js" => "text/javascript; charset=utf-8",
            "css" => "text/css; charset=utf-8",
            "svg" => "image/svg+xml",
            "png" => "image/png",
            "woff2" => "font/woff2",
            _ => "application/octet-stream",
        };
        let absolute = file.canonicalize().unwrap();
        source.push_str(&format!(
            "{relative:?} => Some(({mime:?}, include_bytes!({:?}))),\n",
            absolute.to_str().unwrap()
        ));
    }
    source.push_str("_ => None, } }\n");
    fs::write(
        Path::new(&env::var("OUT_DIR").unwrap()).join("frontend.rs"),
        source,
    )
    .unwrap();
}

fn collect(path: &Path, files: &mut Vec<std::path::PathBuf>) {
    for entry in fs::read_dir(path).unwrap() {
        let entry = entry.unwrap();
        if entry.file_type().unwrap().is_dir() {
            collect(&entry.path(), files);
        } else if entry.file_type().unwrap().is_file() {
            files.push(entry.path());
        }
    }
}
