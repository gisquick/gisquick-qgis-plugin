package gisquick

import (
	"crypto/sha1"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"

	ignore "github.com/sabhiram/go-gitignore"
)

type FileInfo struct {
	Path  string `json:"path"`
	Hash  string `json:"hash"`
	Size  int64  `json:"size"`
	Mtime int64  `json:"mtime"`
}

// Computes SHA-1 hash of file
func Sha1(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	h := sha1.New()
	if _, err := io.Copy(h, file); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", h.Sum(nil)), nil
}

// Computes hash of the file (SHA-1 or dbhash)
func (c *Client) Checksum(path string) (string, error) {
	if c.dbhashCmd != "" && strings.ToLower(filepath.Ext(path)) == ".gpkg" {
		cmdOut, err := exec.Command(c.dbhashCmd, path).Output()
		if err != nil { // errors.Is(err, exec.ErrNotFound)
			return "", fmt.Errorf("executing dbhash command: %w", err)
		}
		hash := strings.Split(string(cmdOut), " ")[0]
		return "dbhash:" + hash, nil
	}
	return Sha1(path)
}

// Collects information about files in given directory
func (c *Client) ListDir(root string, checksum bool) ([]FileInfo, []FileInfo, error) {
	var files []FileInfo = []FileInfo{}
	var tempFiles []FileInfo = []FileInfo{}
	temporaryFileRegex := regexp.MustCompile(`(?i).*\.(gpkg-wal|gpkg-shm)$`)
	excludedDir := ".gisquick" + string(filepath.Separator)
	defaultFileFilter := func(path string) bool {
		return !strings.HasSuffix(path, "~") && !strings.HasPrefix(path, excludedDir)
	}
	fileFilter := defaultFileFilter

	matcher, err := ignore.CompileIgnoreFile(filepath.Join(root, ".gisquickignore"))
	if err == nil {
		fileFilter = func(path string) bool {
			return defaultFileFilter(path) && !matcher.MatchesPath(path)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return files, tempFiles, fmt.Errorf("parsing .gisquickignore file: %w", err)
	}

	root, _ = filepath.Abs(root)
	err = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				fmt.Printf("WARN: file does not exists, skipping: %s\n", path)
				return nil
			}
			return err
		}
		if !info.IsDir() {
			relPath := path[len(root)+1:]
			if fileFilter(relPath) {
				size := info.Size()
				mtime := info.ModTime().Unix()
				if temporaryFileRegex.Match([]byte(relPath)) {
					tempFiles = append(tempFiles, FileInfo{relPath, "", size, mtime})
				} else {
					hash := ""
					if checksum {
						item, inCache := c.checksumCache[path]
						if inCache && item.Mtime == mtime && item.Size == size {
							hash = item.Hash
						} else {
							if hash, err = c.Checksum(path); err != nil {
								return err
							}
							c.checksumCache[path] = FileInfo{Hash: hash, Size: size, Mtime: mtime}
						}
					}
					files = append(files, FileInfo{relPath, hash, size, mtime})
				}
			}
		}
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	return files, tempFiles, nil
}

// Saves content from given reader into the file
func SaveToFile(src io.Reader, filename string) (err error) {
	err = os.MkdirAll(filepath.Dir(filename), os.ModePerm)
	if err != nil {
		return err
	}
	file, err := os.Create(filename)
	if err != nil {
		return err
	}

	// more verbose but with better errors propagation
	defer func() {
		if cerr := file.Close(); cerr != nil && err == nil {
			err = cerr
		}
	}()

	if _, err := io.Copy(file, src); err != nil {
		return err
	}
	return nil
}

// Writes content of the file into given writer
func CopyFile(dest io.Writer, path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	_, err = io.Copy(dest, file)
	return err
}
